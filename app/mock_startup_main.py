from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .daily_log_file import DailyDatedFileHandler
from .openai_schema import (
    ChatCompletionRequest,
    ChatMessage,
    ResponsesCreateRequest,
    flatten_messages,
    make_chat_completion_chunk,
    make_chat_completion_response,
    make_models_response,
    make_response_object,
    make_responses_output_item_added_message,
    make_responses_output_item_done_message,
    make_responses_output_text_delta,
    make_responses_stream_completed,
    make_responses_stream_created,
    responses_input_to_chat_messages,
)
from .sse_frame import sse_comment, sse_data


def _parse_log_level(raw: str | None) -> int:
    name = (raw or "info").strip().upper()
    return getattr(logging, name, logging.INFO)


def _setup_mock_logging() -> None:
    """独立跑 ``mock_startup_main`` 时自带 Handler：控制台 + 可选按日滚动文件（``logs/mock-replay.log``）。"""
    log = logging.getLogger("mock-cursor-wrapper")
    if log.handlers:
        return
    level = _parse_log_level(os.getenv("MOCK_WRAPPER_LOG_LEVEL", os.getenv("WRAPPER_LOG_LEVEL", "debug")))
    log.setLevel(level)
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] [%(name)s] %(filename)s:%(lineno)d %(message)s"
    )

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(level)
    stream_handler.setFormatter(formatter)
    log.addHandler(stream_handler)

    file_disabled = os.getenv("MOCK_LOG_TO_FILE", "1").strip().lower() in {"0", "false", "no", "off"}
    if not file_disabled:
        log_dir = Path(__file__).resolve().parent.parent / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = DailyDatedFileHandler(
            log_dir,
            "mock-replay",
            file_suffix=".log",
            backup_days=30,
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        log.addHandler(file_handler)

    log.propagate = False


_setup_mock_logging()
logger = logging.getLogger("mock-cursor-wrapper")

_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "mock" / "mock_startup_outputs.json"

# 与 ``scripts/build_mock_startup_from_log.py`` 写入的 ``content`` 格式一致；``body`` 可含换行。
_CHUNK_STEP_RE = re.compile(r"^n=(\d+) len=(\d+) body=(.*)\Z", re.DOTALL)


class MockStartupStep(BaseModel):
    model_config = ConfigDict(extra="ignore")

    event: Literal["chunk", "keepalive"] = Field(
        default="chunk",
        description="chunk 对应 stream chunk 日志；keepalive 对应 chars=0 的 keepalive",
    )
    delay_before_s: float = Field(ge=0, description="相对上一事件结束后的等待秒数")
    content: str | None = Field(default=None, description="仅 chunk：与日志 n=len= body= 一致")
    chunks: int = Field(default=0, ge=0, description="仅 keepalive：与日志 chunks= 一致")
    chars: int = Field(default=0, ge=0, description="仅 keepalive：与日志 chars= 一致")

    @model_validator(mode="after")
    def _validate_by_event(self) -> MockStartupStep:
        if self.event == "chunk":
            if self.content is None:
                raise ValueError("chunk 步骤需要 content")
            if _CHUNK_STEP_RE.match(self.content) is None:
                raise ValueError("chunk 步骤 content 须为 n=… len=… body=… 格式")
        return self


class MockSource(BaseModel):
    model_config = ConfigDict(extra="ignore")

    completion_id: str | None = None


class MockStartupFile(BaseModel):
    model_config = ConfigDict(extra="ignore")

    version: int = 1
    title: str | None = None
    description: str | None = None
    source: MockSource | None = Field(
        default=None,
        description="与 scripts/build_mock_startup_from_log.py 生成字段对齐；其中 completion_id 仅作溯源",
    )
    steps: list[MockStartupStep] = Field(
        ...,
        description="chunk 步为 n=len= body=；keepalive 步为 chunks/chars，与 main 中 sse_comment 一致",
    )

    sse_replay: bool = Field(
        default=True,
        description="为 True 时启动阶段额外打印与真实流式一致的 data: / : 行",
    )
    include_role_chunk: bool = Field(
        default=True,
        description="是否在首条正文前发送首包 assistant role（与 main 流式首包一致）",
    )
    include_stop_and_done: bool = Field(
        default=True,
        description="是否在 steps 结束后发送 finish_reason=stop 与 data: [DONE]",
    )
    replay_model: str = Field(
        default="cursor-agent",
        description="未在请求中指定 model 时使用的对外模型名",
    )
    tail_silence_before_done_s: float = Field(
        default=0.0,
        ge=0,
        description=(
            "最后一个 chunk 与 stop/[DONE] 之间的静默秒数，对应 app.main 在 _reap_after_result_event "
            "等收尾工作上花费的时间。OpenClaw/TT 等下游会以这段静默作为提前 flush 的契机；若为 0，"
            "下游会把末尾内容一次性塞进上一条 TT 消息，与真实运行的切分不一致。"
        ),
    )

    @field_validator("steps")
    @classmethod
    def non_empty_steps(cls, v: list[MockStartupStep]) -> list[MockStartupStep]:
        if not v:
            raise ValueError("steps 不能为空")
        return v


def _load_mock_config(path: Path) -> MockStartupFile:
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)
    return MockStartupFile.model_validate(data)


def _delta_body_from_step_content(content: str) -> str | None:
    m = _CHUNK_STEP_RE.match(content)
    if not m:
        return None
    declared = int(m.group(2))
    body = m.group(3)
    if len(body) != declared:
        logger.warning(
            "mock chunk len 与声明不一致 n=%s declared=%s actual=%s；"
            "请用 scripts/build_mock_startup_from_log.py 重新生成配置",
            m.group(1),
            declared,
            len(body),
        )
    return body


def _replay_skip_delays() -> bool:
    return os.getenv("MOCK_REPLAY_SKIP_DELAYS", "").strip().lower() in {"1", "true", "yes", "on"}


def _preview_cap() -> int | None:
    raw = os.getenv("MOCK_LOG_RESPONSE_PREVIEW_MAX_LEN", "").strip()
    if not raw:
        return None
    try:
        n = int(raw, 10)
    except ValueError:
        return None
    return None if n <= 0 else n


def _log_chat_request_debug(body: ChatCompletionRequest, prompt: str, model: str) -> None:
    """与 ``app.main`` 的 ``/v1/chat/completions`` 入口 debug 对齐（logger 名为 mock-cursor-wrapper 便于过滤）。"""
    logger.debug("===== /v1/chat/completions =====")
    logger.debug("model (requested): %s -> (resolved): %s", model, model)
    logger.debug("stream: %s", body.stream)
    logger.debug("messages (%d):", len(body.messages))
    for i, msg in enumerate(body.messages):
        text = msg.content if isinstance(msg.content, str) else str(msg.content)
        logger.debug("  [%d] %s: %s", i, msg.role, text[:200])
    logger.debug("prompt (mock replay, not sent to CLI):\n%s", prompt[:500])


def _log_responses_request_debug(
    body: ResponsesCreateRequest, messages: list[ChatMessage], prompt: str, model: str
) -> None:
    """与 ``app.main`` 的 ``/v1/responses`` 入口 debug 对齐。"""
    logger.debug("===== /v1/responses =====")
    logger.debug("model (requested): %s -> (resolved): %s", model, model)
    logger.debug("stream: %s", body.stream)
    logger.debug("messages (%d):", len(messages))
    for i, msg in enumerate(messages):
        text = msg.content if isinstance(msg.content, str) else str(msg.content)
        logger.debug("  [%d] %s: %s", i, msg.role, text[:200])
    logger.debug("prompt (mock replay, not sent to CLI):\n%s", prompt[:500])


async def async_iter_replay_sse(
    cfg: MockStartupFile,
    completion_id: str,
    model: str,
    *,
    skip_delays: bool,
    debug_log: logging.Logger | None = None,
) -> AsyncIterator[str]:
    """按配置产出与 ``app.main`` 流式一致的 SSE 片段（含 role / keepalive 注释 / delta / stop / [DONE]）。"""
    keepalive_sec = float(os.getenv("CURSOR_STREAM_KEEPALIVE_SECONDS", "12"))
    n_chunks = 0
    total_chars = 0
    last_progress_log = 0
    collected: list[str] = []

    if cfg.include_role_chunk:
        yield sse_data(make_chat_completion_chunk(completion_id, model, role="assistant"))
    if debug_log:
        debug_log.debug(
            "stream started completion_id=%s keepalive_s=%s",
            completion_id,
            keepalive_sec,
        )

    for step in cfg.steps:
        if not skip_delays and step.delay_before_s > 0:
            await asyncio.sleep(step.delay_before_s)
        if step.event == "keepalive":
            yield sse_comment(
                f"cursor-wrapper keepalive chunks={step.chunks} chars={step.chars}"
            )
            if debug_log:
                debug_log.debug(
                    "stream keepalive completion_id=%s chunks=%d chars=%d",
                    completion_id,
                    step.chunks,
                    step.chars,
                )
            continue
        body = _delta_body_from_step_content(step.content or "")
        if body is None:
            continue
        n_chunks += 1
        chunk_len = len(body)
        total_chars += chunk_len
        collected.append(body)
        if debug_log:
            debug_log.debug(
                "stream chunk completion_id=%s n=%d len=%d body=%s",
                completion_id,
                n_chunks,
                chunk_len,
                body,
            )
            if total_chars - last_progress_log >= 8192:
                last_progress_log = total_chars
                debug_log.debug(
                    "stream progress completion_id=%s chunks=%d chars=%d",
                    completion_id,
                    n_chunks,
                    total_chars,
                )
        yield sse_data(make_chat_completion_chunk(completion_id, model, content=body))

    if cfg.include_stop_and_done:
        tail = cfg.tail_silence_before_done_s
        if tail > 0 and not skip_delays:
            await asyncio.sleep(tail)
            if debug_log:
                debug_log.debug(
                    "stream tail silence completion_id=%s waited_s=%.3f",
                    completion_id,
                    tail,
                )
        yield sse_data(
            make_chat_completion_chunk(completion_id, model, finish_reason="stop")
        )
        yield sse_data("[DONE]")
        if debug_log:
            full_response = "".join(collected)
            cap = _preview_cap()
            preview = full_response if cap is None else full_response[:cap]
            debug_log.debug(
                "stream done completion_id=%s chunks=%d chars=%d preview=%s",
                completion_id,
                n_chunks,
                len(full_response),
                preview,
            )


async def async_iter_replay_responses_sse(
    cfg: MockStartupFile,
    response_id: str,
    item_id: str,
    model: str,
    *,
    skip_delays: bool,
    debug_log: logging.Logger | None = None,
) -> AsyncIterator[str]:
    """按同一 ``steps`` 配置产出与 ``app.main`` ``/v1/responses`` 流式一致的 SSE（Responses 事件序列）。"""
    keepalive_sec = float(os.getenv("CURSOR_STREAM_KEEPALIVE_SECONDS", "12"))
    n_chunks = 0
    total_chars = 0
    last_progress_log = 0
    collected: list[str] = []

    created_at = int(time.time())
    yield sse_data(
        make_responses_stream_created(response_id=response_id, model=model, created_at=created_at)
    )
    yield sse_data(make_responses_output_item_added_message(item_id=item_id, output_index=0))

    if debug_log:
        debug_log.debug(
            "responses stream started response_id=%s item_id=%s keepalive_s=%s",
            response_id,
            item_id,
            keepalive_sec,
        )

    for step in cfg.steps:
        if not skip_delays and step.delay_before_s > 0:
            await asyncio.sleep(step.delay_before_s)
        if step.event == "keepalive":
            yield sse_comment(
                f"cursor-wrapper keepalive chunks={step.chunks} chars={step.chars}"
            )
            if debug_log:
                debug_log.debug(
                    "responses stream keepalive response_id=%s chunks=%d chars=%d",
                    response_id,
                    step.chunks,
                    step.chars,
                )
            continue
        body = _delta_body_from_step_content(step.content or "")
        if body is None:
            continue
        n_chunks += 1
        chunk_len = len(body)
        total_chars += chunk_len
        collected.append(body)
        if debug_log:
            debug_log.debug(
                "responses stream chunk response_id=%s n=%d len=%d body=%s",
                response_id,
                n_chunks,
                chunk_len,
                body,
            )
            if total_chars - last_progress_log >= 8192:
                last_progress_log = total_chars
                debug_log.debug(
                    "responses stream progress response_id=%s chunks=%d chars=%d",
                    response_id,
                    n_chunks,
                    total_chars,
                )
        yield sse_data(make_responses_output_text_delta(item_id=item_id, delta=body))

    if cfg.include_stop_and_done:
        tail = cfg.tail_silence_before_done_s
        if tail > 0 and not skip_delays:
            await asyncio.sleep(tail)
            if debug_log:
                debug_log.debug(
                    "responses stream tail silence response_id=%s waited_s=%.3f",
                    response_id,
                    tail,
                )
        full_response = "".join(collected)
        yield sse_data(
            make_responses_output_item_done_message(
                item_id=item_id,
                full_text=full_response,
                output_index=0,
            )
        )
        yield sse_data(make_responses_stream_completed(response_id=response_id, model=model))
        if debug_log:
            cap = _preview_cap()
            preview = full_response if cap is None else full_response[:cap]
            debug_log.debug(
                "responses stream done response_id=%s chunks=%d chars=%d preview=%s",
                response_id,
                n_chunks,
                len(full_response),
                preview,
            )


def _concat_chunk_text(cfg: MockStartupFile) -> str:
    parts: list[str] = []
    for step in cfg.steps:
        if step.event != "chunk":
            continue
        body = _delta_body_from_step_content(step.content or "")
        if body:
            parts.append(body)
    return "".join(parts)


def get_mock_cfg(request: Request) -> MockStartupFile:
    cfg = getattr(request.app.state, "mock_replay_cfg", None)
    if isinstance(cfg, MockStartupFile):
        return cfg
    path = Path(getattr(request.app.state, "mock_startup_config_path", "") or "")
    if not path.is_file():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Mock 回放配置未加载或文件不存在。",
        )
    return _load_mock_config(path)


async def _emit_configured_outputs(cfg: MockStartupFile, config_path: Path) -> None:
    if cfg.title:
        logger.info("mock replay startup: loaded title=%s path=%s", cfg.title, config_path)
    if cfg.description:
        logger.info("mock replay startup: description=%s", cfg.description)

    completion_id = os.getenv("MOCK_SSE_COMPLETION_ID", "").strip() or f"chatcmpl-{uuid.uuid4().hex}"
    model = os.getenv("MOCK_SSE_MODEL", "").strip() or cfg.replay_model

    async for _ in async_iter_replay_sse(
        cfg,
        completion_id,
        model,
        skip_delays=_replay_skip_delays(),
        debug_log=logger if cfg.sse_replay else None,
    ):
        pass

    logger.info(
        "mock replay startup: finished wire replay completion_id=%s steps=%d",
        completion_id,
        len(cfg.steps),
    )


def _replay_on_startup_enabled() -> bool:
    """默认不在进程启动时跑一遍回放（避免无请求也 sleep/打日志）；需要旧行为时设 ``MOCK_REPLAY_ON_STARTUP=1``。"""
    return os.getenv("MOCK_REPLAY_ON_STARTUP", "").strip().lower() in {"1", "true", "yes", "on"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    config_path = Path(app.state.mock_startup_config_path)
    try:
        cfg = _load_mock_config(config_path)
        app.state.mock_replay_cfg = cfg
        on_start = _replay_on_startup_enabled()
        logger.info(
            "mock replay service: config loaded steps=%d path=%s (startup_replay=%s; replay on POST /v1/chat/completions and /v1/responses)",
            len(cfg.steps),
            config_path,
            on_start,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("mock replay service: failed to load config path=%s err=%s", config_path, exc)
        app.state.mock_replay_cfg = None
        yield
        return

    task: asyncio.Task[None] | None = None
    if on_start:
        task = asyncio.create_task(_emit_configured_outputs(cfg, config_path))
    yield
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def create_app(config_path: Path | None = None) -> FastAPI:
    path = config_path or _DEFAULT_CONFIG_PATH
    application = FastAPI(
        title="Mock replay OpenAI API",
        version="0.2.0",
        lifespan=lifespan,
    )
    application.state.mock_startup_config_path = str(path.resolve())
    application.state.mock_replay_cfg = None

    @application.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok", "service": "mock-replay-api"}

    @application.get("/v1/models")
    async def list_models(request: Request) -> dict:
        cfg = get_mock_cfg(request)
        return make_models_response([cfg.replay_model])

    @application.post("/v1/chat/completions")
    async def chat_completions(request: Request, body: ChatCompletionRequest):
        cfg = get_mock_cfg(request)
        prompt = flatten_messages(body.messages)
        model = (body.model or "").strip() or cfg.replay_model
        _log_chat_request_debug(body, prompt, model)
        if not prompt.strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No text messages were provided.",
            )
        completion_id = f"chatcmpl-{uuid.uuid4().hex}"
        skip = _replay_skip_delays()

        if body.stream:

            async def event_stream() -> AsyncIterator[str]:
                async for piece in async_iter_replay_sse(
                    cfg,
                    completion_id,
                    model,
                    skip_delays=skip,
                    debug_log=logger,
                ):
                    yield piece

            return StreamingResponse(
                event_stream(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        text = _concat_chunk_text(cfg)
        logger.info("response (%d chars): %s", len(text), text[:300])
        response = make_chat_completion_response(
            text=text,
            model=model,
            request_id=completion_id,
        )
        response["created"] = int(time.time())
        return response

    @application.post("/v1/responses")
    async def create_response(request: Request, body: ResponsesCreateRequest):
        cfg = get_mock_cfg(request)
        messages = responses_input_to_chat_messages(body.input)
        if not messages:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No usable messages were parsed from input.",
            )
        prompt = flatten_messages(messages)
        model = (body.model or "").strip() or cfg.replay_model
        _log_responses_request_debug(body, messages, prompt, model)
        if not prompt.strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No text messages were provided.",
            )
        response_id = os.getenv("MOCK_SSE_RESPONSE_ID", "").strip() or f"resp_{uuid.uuid4().hex}"
        item_id = os.getenv("MOCK_SSE_RESPONSE_ITEM_ID", "").strip() or f"msg_{uuid.uuid4().hex}"
        skip = _replay_skip_delays()

        if body.stream:

            async def responses_event_stream() -> AsyncIterator[str]:
                async for piece in async_iter_replay_responses_sse(
                    cfg,
                    response_id,
                    item_id,
                    model,
                    skip_delays=skip,
                    debug_log=logger,
                ):
                    yield piece

            return StreamingResponse(
                responses_event_stream(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        text = _concat_chunk_text(cfg)
        logger.info("responses (%d chars): %s", len(text), text[:300])
        return make_response_object(text=text, model=model, response_id=response_id, created_at=int(time.time()))

    return application


_env_config = os.getenv("MOCK_STARTUP_CONFIG", "").strip()
app = create_app(Path(_env_config) if _env_config else None)
