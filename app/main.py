from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .agent_scheduler import (
    AgentFallbackNotice,
    AgentScheduler,
    format_fallback_user_text,
)
from .cli_base import CLIError
from .config import (
    WRAPPER_RESPONSE_GREETING_TEXT,
    Settings,
    get_settings,
)
from .cursor_cli import CursorCLIAdapter, CursorCLIError
from .daily_log_file import DailyDatedFileHandler
from .sse_frame import sse_comment as _sse_comment, sse_data as _sse_data
from .stream_chat_session import StreamChatSession
from .stream_observe import StreamObserveContext
from .stream_timeout import (
    STREAM_AGENT_KILLED_MESSAGE,
    content_idle_exceeded,
    kill_stream_cli_session,
    queue_wait_seconds_for_content_idle,
    resolve_effective_stream_idle_seconds,
    response_ack_idle_force_remaining_seconds,
)
from .openai_schema import (
    ChatCompletionRequest,
    ResponsesCreateRequest,
    build_cli_prompt,
    make_chat_completion_chunk,
    make_chat_completion_response,
    make_error_response,
    make_models_response,
    make_response_object,
    greeting_output_item_events,
    make_responses_content_part_added,
    make_responses_output_item_added_message,
    make_responses_output_item_done_message,
    make_responses_output_text_delta,
    make_responses_stream_completed,
    make_responses_stream_created,
    make_responses_stream_error_event,
    responses_input_to_chat_messages,
)

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_FILE_PREFIX = "wrapper"
LOG_FILE_SUFFIX = ".log"
LOG_BACKUP_DAYS = 30
MAX_LOGGED_BODY_CHARS = 1_000_000


def _setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    settings = get_settings()
    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] [%(name)s] %(filename)s:%(lineno)d %(message)s"
    )

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    stream_handler.setLevel(log_level)

    file_handler = DailyDatedFileHandler(
        LOG_DIR,
        LOG_FILE_PREFIX,
        file_suffix=LOG_FILE_SUFFIX,
        backup_days=LOG_BACKUP_DAYS,
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(log_level)

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    root_logger.handlers.clear()
    root_logger.addHandler(stream_handler)
    root_logger.addHandler(file_handler)

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers.clear()
        uv_logger.addHandler(stream_handler)
        uv_logger.addHandler(file_handler)
        uv_logger.propagate = False


_setup_logging()
logger = logging.getLogger("cursor-wrapper")

# 用于确认 uvicorn 已加载含 stream_observe 的代码（日志里 chunk 行号应为 ~531，而非旧版 ~492）
STREAM_OBSERVE_BUILD = "stream-observe-20260516"

app = FastAPI(title="Cursor OpenAI Wrapper", version="0.1.0")


@app.on_event("startup")
async def _log_stream_observe_build() -> None:
    logger.info(
        "cursor-wrapper started build=%s stream_observe=enabled "
        "(grep log for 'observe' or 'keepalive emit'; chunk log line ~531 in main.py)",
        STREAM_OBSERVE_BUILD,
    )
bearer_scheme = HTTPBearer(auto_error=False)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    try:
        raw_body = await request.body()
        body_preview = raw_body.decode("utf-8", errors="replace")
    except Exception as read_exc:  # noqa: BLE001
        body_preview = f"<failed to read body: {read_exc}>"

    if len(body_preview) > MAX_LOGGED_BODY_CHARS:
        body_preview = (
            body_preview[:MAX_LOGGED_BODY_CHARS]
            + f"...<truncated, total {len(body_preview)} chars>"
        )

    logger.warning(
        "422 validation error on %s %s\nbody=%s\nerrors=%s",
        request.method,
        request.url.path,
        body_preview,
        exc.errors(),
    )
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": exc.errors()},
    )


def get_cursor_cli(settings: Settings = Depends(get_settings)) -> CursorCLIAdapter:
    return CursorCLIAdapter(settings)


def get_agent_scheduler(settings: Settings = Depends(get_settings)) -> AgentScheduler:
    return AgentScheduler(settings)


def require_api_key(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    settings: Settings = Depends(get_settings),
) -> None:
    if not settings.wrapper_api_key:
        return

    token = credentials.credentials if credentials else None
    if token != settings.wrapper_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )


@app.get("/healthz")
async def healthz(settings: Settings = Depends(get_settings)) -> dict:
    scheduler = AgentScheduler(settings)
    schedule_status = scheduler.cli_status_for_schedule()
    return {
        "status": "ok" if schedule_status["any_available"] else "degraded",
        "agent_schedule": schedule_status,
        "cursor_cli": CursorCLIAdapter(settings).cli_status(),
    }


@app.get("/v1/models", dependencies=[Depends(require_api_key)])
async def list_models(
    cursor_cli: CursorCLIAdapter = Depends(get_cursor_cli),
) -> dict:
    try:
        models = await cursor_cli.list_available_models()
    except CursorCLIError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    return make_models_response(models)


@app.post("/v1/chat/completions", dependencies=[Depends(require_api_key)])
async def chat_completions(
    request: ChatCompletionRequest,
    http_request: Request,
    settings: Settings = Depends(get_settings),
    agent_scheduler: AgentScheduler = Depends(get_agent_scheduler),
):
    requested_model = settings.exposed_model(request.model)
    cursor_model = settings.resolve_model(request.model)

    if request.stream:
        completion_id = f"chatcmpl-{uuid.uuid4().hex}"
        collected_chunks: list[str] = []
        keepalive_sec = float(os.getenv("CURSOR_STREAM_KEEPALIVE_SECONDS", "12"))
        idle_seconds = resolve_effective_stream_idle_seconds(
            settings.stream_idle_seconds,
            http_request.headers,
            margin_seconds=settings.stream_upstream_timeout_margin,
            stream_id=completion_id,
        ).effective

        async def event_stream():
            # 先 flush 问候/首包，再跑 build_cli_prompt（含桥接写盘）与 CLI，避免首字被前置逻辑拖住
            if settings.response_greeting:
                collected_chunks.append(WRAPPER_RESPONSE_GREETING_TEXT)
                yield _sse_data(
                    make_chat_completion_chunk(
                        completion_id,
                        requested_model,
                        role="assistant",
                        content=WRAPPER_RESPONSE_GREETING_TEXT,
                    )
                )
                logger.info("已输出greeting文案")
                yield _sse_data(
                    make_chat_completion_chunk(completion_id, requested_model, role="assistant")
                )

            prompt = build_cli_prompt(
                request.messages,
                last_user_context_bridge=settings.cli_last_user_context_bridge,
                persist_full_context=agent_scheduler.persist_full_prompt_for_bridge
                if settings.cli_last_user_context_bridge
                else None,
            )

            logger.debug("===== /v1/chat/completions =====")
            logger.debug("model (requested): %s -> (resolved): %s", requested_model, cursor_model)
            logger.debug("stream: %s", request.stream)
            logger.debug("messages (%d):", len(request.messages))
            for i, msg in enumerate(request.messages):
                text = msg.content if isinstance(msg.content, str) else str(msg.content)
                logger.debug("  [%d] %s: %s", i, msg.role, text[:200])
            logger.debug("prompt sent to CLI:\n%s", prompt[:500])

            if not prompt:
                yield _sse_data(
                    make_error_response("No text messages were provided.", error_type="invalid_request_error")
                )
                yield _sse_data("[DONE]")
                return

            queue: asyncio.Queue[tuple[str, str | None]] = asyncio.Queue()
            cli_session = StreamChatSession()

            observe = StreamObserveContext(
                stream_name="stream",
                stream_id=completion_id,
                keepalive_sec=keepalive_sec,
            )

            async def producer() -> None:
                try:
                    if settings.agent_schedule:
                        await queue.put(("agent", settings.agent_schedule[0]))
                    async for item in agent_scheduler.stream_with_fallback(
                        prompt, cursor_model, session=cli_session
                    ):
                        if isinstance(item, AgentFallbackNotice):
                            await queue.put(("fallback", (item.agent, item.message)))
                            observe.log_producer_put(
                                "fallback", payload_len=len(item.message), qsize=queue.qsize()
                            )
                            idx = list(settings.agent_schedule).index(item.agent) + 1
                            if idx < len(settings.agent_schedule):
                                await queue.put(("agent", settings.agent_schedule[idx]))
                        else:
                            await queue.put(("delta", item))
                            observe.log_producer_put("delta", payload_len=len(item), qsize=queue.qsize())
                except CLIError as exc:
                    await queue.put(("err", str(exc)))
                    observe.log_producer_put("err", payload_len=len(str(exc)), qsize=queue.qsize())
                except Exception as exc:  # noqa: BLE001
                    logger.exception("stream producer failed completion_id=%s", completion_id)
                    await queue.put(("err", str(exc)))
                    observe.log_producer_put("err", payload_len=len(str(exc)), qsize=queue.qsize())
                finally:
                    await queue.put(("eof", None))
                    observe.log_producer_put("eof", qsize=queue.qsize())

            prod = asyncio.create_task(producer())
            logger.info(
                "CLI producer 已启动 completion_id=%s model=%s schedule=%s",
                completion_id,
                cursor_model,
                settings.agent_schedule,
            )
            cursor_content_started = False
            ack_emitted = False
            active_agent = settings.agent_schedule[0] if settings.agent_schedule else "cursor"
            current_ack_text = settings.ack_text_for(active_agent)
            cursor_wait_started_at = time.monotonic()
            total_chars = 0
            n_chunks = 0
            last_progress_log = 0
            timed_out = False
            last_content_push_at = time.monotonic()
            try:
                logger.debug(
                    "stream started completion_id=%s keepalive_s=%s idle_seconds=%s ack_idle_force=%s ack_idle_s=%s",
                    completion_id,
                    keepalive_sec,
                    idle_seconds,
                    settings.response_ack_idle_force,
                    settings.response_ack_idle,
                )
                while True:
                    if content_idle_exceeded(last_content_push_at, idle_seconds):
                        await kill_stream_cli_session(
                            cli_session,
                            prod,
                            stream_id=completion_id,
                            idle_seconds=idle_seconds,
                        )
                        timed_out = True
                        break

                    ack_force_remaining = response_ack_idle_force_remaining_seconds(
                        force_enabled=settings.response_ack_idle_force,
                        idle_seconds=settings.response_ack_idle,
                        wait_started_at=cursor_wait_started_at,
                        ack_emitted=ack_emitted,
                        cursor_content_started=cursor_content_started,
                    )
                    if (
                        ack_force_remaining is not None
                        and ack_force_remaining <= 0
                        and settings.response_ack
                        and not ack_emitted
                    ):
                        yield _sse_data(
                            make_chat_completion_chunk(
                                completion_id,
                                requested_model,
                                content=current_ack_text,
                            )
                        )
                        ack_emitted = True
                        last_content_push_at = time.monotonic()
                        logger.info(
                            "已输出ack文案(空闲强制) completion_id=%s idle_s=%s",
                            completion_id,
                            settings.response_ack_idle,
                        )

                    observe.log_wait_queue_begin(prod_done=prod.done())
                    wait_sec = queue_wait_seconds_for_content_idle(
                        keepalive_sec, last_content_push_at, idle_seconds
                    )
                    if ack_force_remaining is not None:
                        wait_sec = min(wait_sec, ack_force_remaining)
                    if wait_sec <= 0:
                        await kill_stream_cli_session(
                            cli_session,
                            prod,
                            stream_id=completion_id,
                            idle_seconds=idle_seconds,
                        )
                        timed_out = True
                        break

                    try:
                        kind, payload = await asyncio.wait_for(queue.get(), timeout=wait_sec)
                    except asyncio.TimeoutError:
                        ack_force_remaining = response_ack_idle_force_remaining_seconds(
                            force_enabled=settings.response_ack_idle_force,
                            idle_seconds=settings.response_ack_idle,
                            wait_started_at=cursor_wait_started_at,
                            ack_emitted=ack_emitted,
                            cursor_content_started=cursor_content_started,
                        )
                        if (
                            ack_force_remaining is not None
                            and ack_force_remaining <= 0
                            and settings.response_ack
                            and not ack_emitted
                        ):
                            yield _sse_data(
                                make_chat_completion_chunk(
                                    completion_id,
                                    requested_model,
                                    content=current_ack_text,
                                )
                            )
                            ack_emitted = True
                            last_content_push_at = time.monotonic()
                            logger.info(
                                "已输出ack文案(空闲强制) completion_id=%s idle_s=%s",
                                completion_id,
                                settings.response_ack_idle,
                            )
                            continue
                        if content_idle_exceeded(last_content_push_at, idle_seconds):
                            await kill_stream_cli_session(
                                cli_session,
                                prod,
                                stream_id=completion_id,
                                idle_seconds=idle_seconds,
                            )
                            timed_out = True
                            break
                        observe.log_queue_idle_timeout()
                        observe.log_keepalive_emit_begin()
                        yield _sse_comment(
                            f"cursor-wrapper keepalive chunks={n_chunks} chars={total_chars}"
                        )
                        observe.log_keepalive_emit_end(chunks=n_chunks, chars=total_chars)
                        last_content_push_at = time.monotonic()
                        logger.debug(
                            "stream keepalive completion_id=%s chunks=%d chars=%d",
                            completion_id,
                            n_chunks,
                            total_chars,
                        )
                        continue

                    observe.log_queue_item(
                        kind, payload_len=len(payload) if payload is not None else None
                    )
                    if kind == "eof":
                        break
                    if kind == "err":
                        assert payload is not None
                        logger.error(
                            "stream error completion_id=%s agent=%s: %s",
                            completion_id,
                            active_agent,
                            payload,
                        )
                        yield _sse_data(make_error_response(payload))
                        last_content_push_at = time.monotonic()
                        yield _sse_data("[DONE]")
                        return

                    if kind == "agent":
                        assert payload is not None
                        active_agent = str(payload)
                        current_ack_text = settings.ack_text_for(active_agent)
                        continue

                    if kind == "fallback":
                        assert payload is not None
                        agent_name, err_msg = payload
                        fallback_text = format_fallback_user_text(agent_name, err_msg)
                        collected_chunks.append(fallback_text)
                        yield _sse_data(
                            make_chat_completion_chunk(
                                completion_id,
                                requested_model,
                                content=fallback_text,
                            )
                        )
                        last_content_push_at = time.monotonic()
                        logger.warning(
                            "agent 降级说明已输出 completion_id=%s agent=%s",
                            completion_id,
                            agent_name,
                        )
                        continue

                    assert kind == "delta" and payload is not None
                    if not cursor_content_started:
                        logger.info(
                            "收到首个 agent delta completion_id=%s agent=%s len=%d",
                            completion_id,
                            active_agent,
                            len(payload),
                        )
                        if settings.response_ack and not ack_emitted:
                            yield _sse_data(
                                make_chat_completion_chunk(
                                    completion_id,
                                    requested_model,
                                    content=current_ack_text,
                                )
                            )
                            ack_emitted = True
                            logger.info("已输出ack文案 completion_id=%s", completion_id)
                        cursor_content_started = True
                    collected_chunks.append(payload)
                    chunk_len = len(payload)
                    total_chars += chunk_len
                    n_chunks += 1
                    logger.debug(
                        "stream chunk completion_id=%s n=%d len=%d body=%s",
                        completion_id,
                        n_chunks,
                        chunk_len,
                        payload,
                    )
                    if total_chars - last_progress_log >= 8192:
                        last_progress_log = total_chars
                        logger.debug(
                            "stream progress completion_id=%s chunks=%d chars=%d",
                            completion_id,
                            n_chunks,
                            total_chars,
                        )
                    observe.log_sse_delta_begin(n=n_chunks, chunk_len=chunk_len)
                    yield _sse_data(
                        make_chat_completion_chunk(completion_id, requested_model, content=payload)
                    )
                    observe.log_sse_delta_end(n=n_chunks)
                    last_content_push_at = time.monotonic()
            finally:
                observe.log_finally(prod_done=prod.done())
                if not timed_out and not prod.done():
                    prod.cancel()
                    try:
                        await prod
                    except asyncio.CancelledError:
                        pass

            if timed_out:
                collected_chunks.append(STREAM_AGENT_KILLED_MESSAGE)
                yield _sse_data(
                    make_chat_completion_chunk(
                        completion_id,
                        requested_model,
                        content=STREAM_AGENT_KILLED_MESSAGE,
                    )
                )
                yield _sse_data(
                    make_chat_completion_chunk(
                        completion_id,
                        requested_model,
                        finish_reason="stop",
                    )
                )
                yield _sse_data("[DONE]")
                logger.warning(
                    "stream content-idle timeout completion_id=%s agent=%s idle_seconds=%s",
                    completion_id,
                    cli_session.agent,
                    idle_seconds,
                )
                return

            yield _sse_data(
                make_chat_completion_chunk(
                    completion_id,
                    requested_model,
                    finish_reason="stop",
                )
            )
            yield _sse_data("[DONE]")
            full_response = "".join(collected_chunks)
            cap = settings.log_response_preview_max_len
            preview = full_response if cap is None else full_response[:cap]
            logger.debug(
                "stream done completion_id=%s chunks=%d chars=%d preview=%s",
                completion_id,
                n_chunks,
                len(full_response),
                preview,
            )

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    prompt = build_cli_prompt(
        request.messages,
        last_user_context_bridge=settings.cli_last_user_context_bridge,
        persist_full_context=agent_scheduler.persist_full_prompt_for_bridge
        if settings.cli_last_user_context_bridge
        else None,
    )

    logger.debug("===== /v1/chat/completions =====")
    logger.debug("model (requested): %s -> (resolved): %s", requested_model, cursor_model)
    logger.debug("stream: %s", request.stream)
    logger.debug("messages (%d):", len(request.messages))
    for i, msg in enumerate(request.messages):
        text = msg.content if isinstance(msg.content, str) else str(msg.content)
        logger.debug("  [%d] %s: %s", i, msg.role, text[:200])
    logger.debug("prompt sent to CLI:\n%s", prompt[:500])

    if not prompt:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No text messages were provided.")

    try:
        result = await agent_scheduler.run_with_fallback(prompt, cursor_model)
    except CLIError as exc:
        logger.error("non-stream error: %s", exc)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    out_text = result.text
    if settings.response_ack:
        out_text = settings.ack_text_for(result.agent) + out_text
        logger.info("已输出ack文案 agent=%s", result.agent)
    if settings.response_greeting:
        out_text = WRAPPER_RESPONSE_GREETING_TEXT + out_text
        logger.info("已输出greeting文案")

    logger.info("response (%d chars): %s", len(out_text), out_text[:300])

    response = make_chat_completion_response(
        text=out_text,
        model=requested_model,
        request_id=result.request_id,
    )
    response["created"] = int(time.time())
    return response


@app.post("/v1/responses", dependencies=[Depends(require_api_key)])
async def create_response(
    request: ResponsesCreateRequest,
    http_request: Request,
    settings: Settings = Depends(get_settings),
    agent_scheduler: AgentScheduler = Depends(get_agent_scheduler),
):
    """OpenAI Responses API：与 ``/v1/chat/completions`` 共用 CLI、桥接、greeting、流式 keepalive。"""
    messages = responses_input_to_chat_messages(request.input)
    if not messages:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No usable messages were parsed from input.",
        )

    requested_model = settings.exposed_model(request.model)
    cursor_model = settings.resolve_model(request.model)

    if request.stream:
        response_id = f"resp_{uuid.uuid4().hex}"
        cursor_item_id = f"msg_{uuid.uuid4().hex}"
        created_at = int(time.time())
        collected_chunks: list[str] = []
        keepalive_sec = float(os.getenv("CURSOR_STREAM_KEEPALIVE_SECONDS", "12"))
        next_output_index = 1 if settings.response_greeting else 0
        main_output_index: int | None = None
        ack_output_index: int | None = None
        idle_seconds = resolve_effective_stream_idle_seconds(
            settings.stream_idle_seconds,
            http_request.headers,
            margin_seconds=settings.stream_upstream_timeout_margin,
            stream_id=response_id,
        ).effective

        async def event_stream():
            nonlocal next_output_index, main_output_index, ack_output_index

            yield _sse_data(
                make_responses_stream_created(
                    response_id=response_id,
                    model=requested_model,
                    created_at=created_at,
                )
            )

            if settings.response_greeting:
                greeting_item_id = f"msg_{uuid.uuid4().hex}"
                for event in greeting_output_item_events(
                    item_id=greeting_item_id,
                    text=WRAPPER_RESPONSE_GREETING_TEXT,
                    output_index=0,
                ):
                    yield _sse_data(event)
                logger.info("已输出greeting文案(responses)")

            prompt = build_cli_prompt(
                messages,
                last_user_context_bridge=settings.cli_last_user_context_bridge,
                persist_full_context=agent_scheduler.persist_full_prompt_for_bridge
                if settings.cli_last_user_context_bridge
                else None,
            )

            logger.debug("===== /v1/responses =====")
            logger.debug("model (requested): %s -> (resolved): %s", requested_model, cursor_model)
            logger.debug("stream: %s", request.stream)
            logger.debug("messages (%d):", len(messages))
            for i, msg in enumerate(messages):
                text = msg.content if isinstance(msg.content, str) else str(msg.content)
                logger.debug("  [%d] %s: %s", i, msg.role, text[:200])
            logger.debug("prompt sent to CLI:\n%s", prompt[:500])

            if not prompt:
                yield _sse_data(
                    make_responses_stream_error_event("No text messages were provided.")
                )
                return

            queue: asyncio.Queue[tuple[str, str | None]] = asyncio.Queue()
            cli_session = StreamChatSession()

            observe = StreamObserveContext(
                stream_name="responses stream",
                stream_id=response_id,
                keepalive_sec=keepalive_sec,
            )

            async def producer() -> None:
                try:
                    if settings.agent_schedule:
                        await queue.put(("agent", settings.agent_schedule[0]))
                    async for item in agent_scheduler.stream_with_fallback(
                        prompt, cursor_model, session=cli_session
                    ):
                        if isinstance(item, AgentFallbackNotice):
                            await queue.put(("fallback", (item.agent, item.message)))
                            observe.log_producer_put(
                                "fallback", payload_len=len(item.message), qsize=queue.qsize()
                            )
                            idx = list(settings.agent_schedule).index(item.agent) + 1
                            if idx < len(settings.agent_schedule):
                                await queue.put(("agent", settings.agent_schedule[idx]))
                        else:
                            await queue.put(("delta", item))
                            observe.log_producer_put("delta", payload_len=len(item), qsize=queue.qsize())
                except CLIError as exc:
                    await queue.put(("err", str(exc)))
                    observe.log_producer_put("err", payload_len=len(str(exc)), qsize=queue.qsize())
                except Exception as exc:  # noqa: BLE001
                    logger.exception("responses stream producer failed response_id=%s", response_id)
                    await queue.put(("err", str(exc)))
                    observe.log_producer_put("err", payload_len=len(str(exc)), qsize=queue.qsize())
                finally:
                    await queue.put(("eof", None))
                    observe.log_producer_put("eof", qsize=queue.qsize())

            prod = asyncio.create_task(producer())
            logger.info(
                "CLI producer 已启动 response_id=%s model=%s schedule=%s",
                response_id,
                cursor_model,
                settings.agent_schedule,
            )
            cursor_item_opened = False
            ack_emitted = False
            active_agent = settings.agent_schedule[0] if settings.agent_schedule else "cursor"
            current_ack_text = settings.ack_text_for(active_agent)
            cursor_wait_started_at = time.monotonic()
            total_chars = 0
            n_chunks = 0
            last_progress_log = 0
            timed_out = False
            last_content_push_at = time.monotonic()
            try:
                logger.debug(
                    "responses stream started response_id=%s keepalive_s=%s idle_seconds=%s ack_idle_force=%s ack_idle_s=%s",
                    response_id,
                    keepalive_sec,
                    idle_seconds,
                    settings.response_ack_idle_force,
                    settings.response_ack_idle,
                )
                while True:
                    if content_idle_exceeded(last_content_push_at, idle_seconds):
                        await kill_stream_cli_session(
                            cli_session,
                            prod,
                            stream_id=response_id,
                            idle_seconds=idle_seconds,
                        )
                        timed_out = True
                        break

                    ack_force_remaining = response_ack_idle_force_remaining_seconds(
                        force_enabled=settings.response_ack_idle_force,
                        idle_seconds=settings.response_ack_idle,
                        wait_started_at=cursor_wait_started_at,
                        ack_emitted=ack_emitted,
                        cursor_content_started=cursor_item_opened,
                    )
                    if (
                        ack_force_remaining is not None
                        and ack_force_remaining <= 0
                        and settings.response_ack
                        and not ack_emitted
                    ):
                        if ack_output_index is None:
                            ack_output_index = next_output_index
                            next_output_index += 1
                        ack_item_id = f"msg_{uuid.uuid4().hex}"
                        for event in greeting_output_item_events(
                            item_id=ack_item_id,
                            text=current_ack_text,
                            output_index=ack_output_index,
                        ):
                            yield _sse_data(event)
                        ack_emitted = True
                        last_content_push_at = time.monotonic()
                        logger.info(
                            "已输出ack文案(空闲强制/responses) item_id=%s output_index=%s idle_s=%s",
                            ack_item_id,
                            ack_output_index,
                            settings.response_ack_idle,
                        )

                    observe.log_wait_queue_begin(prod_done=prod.done())
                    wait_sec = queue_wait_seconds_for_content_idle(
                        keepalive_sec, last_content_push_at, idle_seconds
                    )
                    if ack_force_remaining is not None:
                        wait_sec = min(wait_sec, ack_force_remaining)
                    if wait_sec <= 0:
                        await kill_stream_cli_session(
                            cli_session,
                            prod,
                            stream_id=response_id,
                            idle_seconds=idle_seconds,
                        )
                        timed_out = True
                        break

                    try:
                        kind, payload = await asyncio.wait_for(queue.get(), timeout=wait_sec)
                    except asyncio.TimeoutError:
                        ack_force_remaining = response_ack_idle_force_remaining_seconds(
                            force_enabled=settings.response_ack_idle_force,
                            idle_seconds=settings.response_ack_idle,
                            wait_started_at=cursor_wait_started_at,
                            ack_emitted=ack_emitted,
                            cursor_content_started=cursor_item_opened,
                        )
                        if (
                            ack_force_remaining is not None
                            and ack_force_remaining <= 0
                            and settings.response_ack
                            and not ack_emitted
                        ):
                            if ack_output_index is None:
                                ack_output_index = next_output_index
                                next_output_index += 1
                            ack_item_id = f"msg_{uuid.uuid4().hex}"
                            for event in greeting_output_item_events(
                                item_id=ack_item_id,
                                text=current_ack_text,
                                output_index=ack_output_index,
                            ):
                                yield _sse_data(event)
                            ack_emitted = True
                            last_content_push_at = time.monotonic()
                            logger.info(
                                "已输出ack文案(空闲强制/responses) item_id=%s output_index=%s idle_s=%s",
                                ack_item_id,
                                ack_output_index,
                                settings.response_ack_idle,
                            )
                            continue
                        if content_idle_exceeded(last_content_push_at, idle_seconds):
                            await kill_stream_cli_session(
                                cli_session,
                                prod,
                                stream_id=response_id,
                                idle_seconds=idle_seconds,
                            )
                            timed_out = True
                            break
                        observe.log_queue_idle_timeout()
                        observe.log_keepalive_emit_begin()
                        yield _sse_comment(
                            f"cursor-wrapper keepalive chunks={n_chunks} chars={total_chars}"
                        )
                        observe.log_keepalive_emit_end(chunks=n_chunks, chars=total_chars)
                        last_content_push_at = time.monotonic()
                        logger.debug(
                            "responses stream keepalive response_id=%s chunks=%d chars=%d",
                            response_id,
                            n_chunks,
                            total_chars,
                        )
                        continue

                    observe.log_queue_item(
                        kind, payload_len=len(payload) if payload is not None else None
                    )
                    if kind == "eof":
                        break
                    if kind == "err":
                        assert payload is not None
                        logger.error(
                            "responses stream error response_id=%s agent=%s: %s",
                            response_id,
                            active_agent,
                            payload,
                        )
                        yield _sse_data(make_responses_stream_error_event(payload))
                        return

                    if kind == "agent":
                        assert payload is not None
                        active_agent = str(payload)
                        current_ack_text = settings.ack_text_for(active_agent)
                        continue

                    if kind == "fallback":
                        assert payload is not None
                        agent_name, err_msg = payload
                        fallback_text = format_fallback_user_text(agent_name, err_msg)
                        fallback_item_id = f"msg_{uuid.uuid4().hex}"
                        for event in greeting_output_item_events(
                            item_id=fallback_item_id,
                            text=fallback_text,
                            output_index=next_output_index,
                        ):
                            yield _sse_data(event)
                        next_output_index += 1
                        last_content_push_at = time.monotonic()
                        logger.warning(
                            "agent 降级说明已输出(responses) response_id=%s agent=%s output_index=%s",
                            response_id,
                            agent_name,
                            next_output_index - 1,
                        )
                        continue

                    assert kind == "delta" and payload is not None
                    if not cursor_item_opened:
                        chunk_len = len(payload)
                        logger.info(
                            "收到首个 agent delta response_id=%s agent=%s len=%d",
                            response_id,
                            active_agent,
                            chunk_len,
                        )
                        if settings.response_ack and not ack_emitted:
                            if ack_output_index is None:
                                ack_output_index = next_output_index
                                next_output_index += 1
                            ack_item_id = f"msg_{uuid.uuid4().hex}"
                            for event in greeting_output_item_events(
                                item_id=ack_item_id,
                                text=current_ack_text,
                                output_index=ack_output_index,
                            ):
                                yield _sse_data(event)
                            ack_emitted = True
                            logger.info(
                                "已输出ack文案(responses) item_id=%s output_index=%s",
                                ack_item_id,
                                ack_output_index,
                            )
                        if main_output_index is None:
                            main_output_index = next_output_index
                            next_output_index += 1
                        yield _sse_data(
                            make_responses_output_item_added_message(
                                item_id=cursor_item_id,
                                output_index=main_output_index,
                            )
                        )
                        yield _sse_data(
                            make_responses_content_part_added(
                                item_id=cursor_item_id,
                                output_index=main_output_index,
                            )
                        )
                        cursor_item_opened = True
                        collected_chunks.append(payload)
                        total_chars += chunk_len
                        n_chunks = 1
                        observe.log_sse_delta_begin(n=n_chunks, chunk_len=chunk_len)
                        yield _sse_data(
                            make_responses_output_text_delta(
                                item_id=cursor_item_id,
                                delta=payload,
                                output_index=main_output_index,
                            )
                        )
                        observe.log_sse_delta_end(n=n_chunks)
                        last_content_push_at = time.monotonic()
                        continue

                    collected_chunks.append(payload)
                    chunk_len = len(payload)
                    total_chars += chunk_len
                    n_chunks += 1
                    logger.debug(
                        "responses stream chunk response_id=%s n=%d len=%d body=%s",
                        response_id,
                        n_chunks,
                        chunk_len,
                        payload,
                    )
                    if total_chars - last_progress_log >= 8192:
                        last_progress_log = total_chars
                        logger.debug(
                            "responses stream progress response_id=%s chunks=%d chars=%d",
                            response_id,
                            n_chunks,
                            total_chars,
                        )
                    observe.log_sse_delta_begin(n=n_chunks, chunk_len=chunk_len)
                    assert main_output_index is not None
                    yield _sse_data(
                        make_responses_output_text_delta(
                            item_id=cursor_item_id,
                            delta=payload,
                            output_index=main_output_index,
                        )
                    )
                    observe.log_sse_delta_end(n=n_chunks)
                    last_content_push_at = time.monotonic()
            finally:
                observe.log_finally(prod_done=prod.done())
                if not timed_out and not prod.done():
                    prod.cancel()
                    try:
                        await prod
                    except asyncio.CancelledError:
                        pass

            if timed_out:
                if not cursor_item_opened:
                    yield _sse_data(
                        make_responses_stream_completed(
                            response_id=response_id, model=requested_model
                        )
                    )
                    logger.warning(
                        "responses stream timeout before first delta response_id=%s idle_seconds=%s",
                        response_id,
                        idle_seconds,
                    )
                    return
                collected_chunks.append(STREAM_AGENT_KILLED_MESSAGE)
                assert main_output_index is not None
                yield _sse_data(
                    make_responses_output_text_delta(
                        item_id=cursor_item_id,
                        delta=STREAM_AGENT_KILLED_MESSAGE,
                        output_index=main_output_index,
                    )
                )
                full_response = "".join(collected_chunks)
                yield _sse_data(
                    make_responses_output_item_done_message(
                        item_id=cursor_item_id,
                        full_text=full_response,
                        output_index=main_output_index,
                    )
                )
                yield _sse_data(
                    make_responses_stream_completed(response_id=response_id, model=requested_model)
                )
                logger.warning(
                    "responses stream content-idle timeout response_id=%s agent=%s idle_seconds=%s",
                    response_id,
                    cli_session.agent,
                    idle_seconds,
                )
                return

            if not cursor_item_opened:
                yield _sse_data(
                    make_responses_stream_completed(
                        response_id=response_id, model=requested_model
                    )
                )
                return

            full_response = "".join(collected_chunks)
            assert main_output_index is not None
            yield _sse_data(
                make_responses_output_item_done_message(
                    item_id=cursor_item_id,
                    full_text=full_response,
                    output_index=main_output_index,
                )
            )
            yield _sse_data(
                make_responses_stream_completed(response_id=response_id, model=requested_model)
            )
            cap = settings.log_response_preview_max_len
            preview = full_response if cap is None else full_response[:cap]
            logger.debug(
                "responses stream done response_id=%s chunks=%d chars=%d preview=%s",
                response_id,
                n_chunks,
                len(full_response),
                preview,
            )

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    prompt = build_cli_prompt(
        messages,
        last_user_context_bridge=settings.cli_last_user_context_bridge,
        persist_full_context=agent_scheduler.persist_full_prompt_for_bridge
        if settings.cli_last_user_context_bridge
        else None,
    )

    logger.debug("===== /v1/responses =====")
    logger.debug("model (requested): %s -> (resolved): %s", requested_model, cursor_model)
    logger.debug("stream: %s", request.stream)
    logger.debug("messages (%d):", len(messages))
    for i, msg in enumerate(messages):
        text = msg.content if isinstance(msg.content, str) else str(msg.content)
        logger.debug("  [%d] %s: %s", i, msg.role, text[:200])
    logger.debug("prompt sent to CLI:\n%s", prompt[:500])

    if not prompt:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No text messages were provided.")

    try:
        result = await agent_scheduler.run_with_fallback(prompt, cursor_model)
    except CLIError as exc:
        logger.error("responses non-stream error: %s", exc)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    out_text = result.text
    if settings.response_ack:
        out_text = settings.ack_text_for(result.agent) + out_text
        logger.info("已输出ack文案(responses) agent=%s", result.agent)
    if settings.response_greeting:
        out_text = WRAPPER_RESPONSE_GREETING_TEXT + out_text
        logger.info("已输出greeting文案(responses)")

    logger.info("responses response (%d chars): %s", len(out_text), out_text[:300])

    response = make_response_object(
        text=out_text,
        model=requested_model,
        response_id=result.request_id,
        created_at=int(time.time()),
    )
    return response
