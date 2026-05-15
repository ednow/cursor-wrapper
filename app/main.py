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

from .config import WRAPPER_RESPONSE_GREETING_TEXT, Settings, get_settings
from .cursor_cli import CursorCLIAdapter, CursorCLIError
from .daily_log_file import DailyDatedFileHandler
from .sse_frame import sse_comment as _sse_comment, sse_data as _sse_data
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

app = FastAPI(title="Cursor OpenAI Wrapper", version="0.1.0")
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
    cursor_cli = CursorCLIAdapter(settings)
    cli_status = cursor_cli.cli_status()
    return {
        "status": "ok" if cli_status["available"] else "degraded",
        "cursor_cli": cli_status,
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
    settings: Settings = Depends(get_settings),
    cursor_cli: CursorCLIAdapter = Depends(get_cursor_cli),
):
    requested_model = settings.exposed_model(request.model)
    cursor_model = settings.resolve_model(request.model)

    if request.stream:
        completion_id = f"chatcmpl-{uuid.uuid4().hex}"
        collected_chunks: list[str] = []
        keepalive_sec = float(os.getenv("CURSOR_STREAM_KEEPALIVE_SECONDS", "12"))

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
                persist_full_context=cursor_cli.persist_full_prompt_for_bridge
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

            async def producer() -> None:
                try:
                    async for text in cursor_cli.stream_chat(prompt, cursor_model):
                        await queue.put(("delta", text))
                except CursorCLIError as exc:
                    await queue.put(("err", str(exc)))
                except Exception as exc:  # noqa: BLE001
                    logger.exception("stream producer failed completion_id=%s", completion_id)
                    await queue.put(("err", str(exc)))
                finally:
                    await queue.put(("eof", None))

            prod = asyncio.create_task(producer())
            total_chars = 0
            n_chunks = 0
            last_progress_log = 0
            try:
                logger.debug(
                    "stream started completion_id=%s keepalive_s=%s",
                    completion_id,
                    keepalive_sec,
                )
                while True:
                    try:
                        kind, payload = await asyncio.wait_for(queue.get(), timeout=keepalive_sec)
                    except asyncio.TimeoutError:
                        yield _sse_comment(
                            f"cursor-wrapper keepalive chunks={n_chunks} chars={total_chars}"
                        )
                        logger.debug(
                            "stream keepalive completion_id=%s chunks=%d chars=%d",
                            completion_id,
                            n_chunks,
                            total_chars,
                        )
                        continue

                    if kind == "eof":
                        break
                    if kind == "err":
                        assert payload is not None
                        logger.error("stream error completion_id=%s: %s", completion_id, payload)
                        yield _sse_data(make_error_response(payload))
                        yield _sse_data("[DONE]")
                        return

                    assert kind == "delta" and payload is not None
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
                    yield _sse_data(
                        make_chat_completion_chunk(completion_id, requested_model, content=payload)
                    )
            finally:
                if not prod.done():
                    prod.cancel()
                    try:
                        await prod
                    except asyncio.CancelledError:
                        pass

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
        persist_full_context=cursor_cli.persist_full_prompt_for_bridge
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
        result = await cursor_cli.run_chat(prompt, cursor_model)
    except CursorCLIError as exc:
        logger.error("non-stream error: %s", exc)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    out_text = result.text
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
    settings: Settings = Depends(get_settings),
    cursor_cli: CursorCLIAdapter = Depends(get_cursor_cli),
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
        body_item_id = f"msg_{uuid.uuid4().hex}"
        created_at = int(time.time())
        collected_chunks: list[str] = []
        keepalive_sec = float(os.getenv("CURSOR_STREAM_KEEPALIVE_SECONDS", "12"))
        body_output_index = 1 if settings.response_greeting else 0

        async def event_stream():
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

            yield _sse_data(
                make_responses_output_item_added_message(
                    item_id=body_item_id,
                    output_index=body_output_index,
                )
            )

            prompt = build_cli_prompt(
                messages,
                last_user_context_bridge=settings.cli_last_user_context_bridge,
                persist_full_context=cursor_cli.persist_full_prompt_for_bridge
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

            async def producer() -> None:
                try:
                    async for text in cursor_cli.stream_chat(prompt, cursor_model):
                        await queue.put(("delta", text))
                except CursorCLIError as exc:
                    await queue.put(("err", str(exc)))
                except Exception as exc:  # noqa: BLE001
                    logger.exception("responses stream producer failed response_id=%s", response_id)
                    await queue.put(("err", str(exc)))
                finally:
                    await queue.put(("eof", None))

            prod = asyncio.create_task(producer())
            total_chars = 0
            n_chunks = 0
            last_progress_log = 0
            try:
                logger.debug(
                    "responses stream started response_id=%s keepalive_s=%s",
                    response_id,
                    keepalive_sec,
                )
                while True:
                    try:
                        kind, payload = await asyncio.wait_for(queue.get(), timeout=keepalive_sec)
                    except asyncio.TimeoutError:
                        yield _sse_comment(
                            f"cursor-wrapper keepalive chunks={n_chunks} chars={total_chars}"
                        )
                        logger.debug(
                            "responses stream keepalive response_id=%s chunks=%d chars=%d",
                            response_id,
                            n_chunks,
                            total_chars,
                        )
                        continue

                    if kind == "eof":
                        break
                    if kind == "err":
                        assert payload is not None
                        logger.error("responses stream error response_id=%s: %s", response_id, payload)
                        yield _sse_data(make_responses_stream_error_event(payload))
                        return

                    assert kind == "delta" and payload is not None
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
                    yield _sse_data(
                        make_responses_output_text_delta(
                            item_id=body_item_id,
                            delta=payload,
                            output_index=body_output_index,
                        )
                    )
            finally:
                if not prod.done():
                    prod.cancel()
                    try:
                        await prod
                    except asyncio.CancelledError:
                        pass

            full_response = "".join(collected_chunks)
            yield _sse_data(
                make_responses_output_item_done_message(
                    item_id=body_item_id,
                    full_text=full_response,
                    output_index=body_output_index,
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
        persist_full_context=cursor_cli.persist_full_prompt_for_bridge
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
        result = await cursor_cli.run_chat(prompt, cursor_model)
    except CursorCLIError as exc:
        logger.error("responses non-stream error: %s", exc)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    out_text = result.text
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
