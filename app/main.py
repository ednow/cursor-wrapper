from __future__ import annotations

import json
import logging
import time
import uuid

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import Settings, get_settings
from .cursor_cli import CursorCLIAdapter, CursorCLIError
from .openai_schema import (
    ChatCompletionRequest,
    flatten_messages,
    make_chat_completion_chunk,
    make_chat_completion_response,
    make_error_response,
    make_models_response,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("cursor-wrapper")

app = FastAPI(title="Cursor OpenAI Wrapper", version="0.1.0")
bearer_scheme = HTTPBearer(auto_error=False)


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


def _sse_data(payload: dict | str) -> str:
    body = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return f"data: {body}\n\n"


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
    prompt = flatten_messages(request.messages)
    requested_model = settings.exposed_model(request.model)
    cursor_model = settings.resolve_model(request.model)

    logger.info("===== /v1/chat/completions =====")
    logger.info("model (requested): %s -> (resolved): %s", requested_model, cursor_model)
    logger.info("stream: %s", request.stream)
    logger.info("messages (%d):", len(request.messages))
    for i, msg in enumerate(request.messages):
        text = msg.content if isinstance(msg.content, str) else str(msg.content)
        logger.info("  [%d] %s: %s", i, msg.role, text[:200])
    logger.info("prompt sent to CLI:\n%s", prompt[:500])

    if not prompt:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No text messages were provided.")

    if request.stream:
        completion_id = f"chatcmpl-{uuid.uuid4().hex}"
        collected_chunks: list[str] = []

        async def event_stream():
            yield _sse_data(
                make_chat_completion_chunk(completion_id, requested_model, role="assistant")
            )
            try:
                async for text in cursor_cli.stream_chat(prompt, cursor_model):
                    collected_chunks.append(text)
                    yield _sse_data(
                        make_chat_completion_chunk(completion_id, requested_model, content=text)
                    )
            except CursorCLIError as exc:
                logger.error("stream error: %s", exc)
                yield _sse_data(make_error_response(str(exc)))
                yield _sse_data("[DONE]")
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
            logger.info("stream response (%d chars): %s", len(full_response), full_response[:300])

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    try:
        result = await cursor_cli.run_chat(prompt, cursor_model)
    except CursorCLIError as exc:
        logger.error("non-stream error: %s", exc)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    logger.info("response (%d chars): %s", len(result.text), result.text[:300])

    response = make_chat_completion_response(
        text=result.text,
        model=requested_model,
        request_id=result.request_id,
    )
    response["created"] = int(time.time())
    return response
