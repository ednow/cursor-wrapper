from __future__ import annotations

import time
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class MessageContentPart(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: str = "text"
    text: str | None = None


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: Literal["system", "user", "assistant"]
    content: str | list[MessageContentPart]


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    model: str | None = None
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None


def message_content_to_text(content: str | list[MessageContentPart]) -> str:
    if isinstance(content, str):
        return content

    parts: list[str] = []
    for part in content:
        if part.type == "text" and part.text:
            parts.append(part.text)
    return "".join(parts)


def flatten_messages(messages: list[ChatMessage]) -> str:
    prompt_parts: list[str] = []
    for message in messages:
        content = message_content_to_text(message.content).strip()
        if not content:
            continue
        prompt_parts.append(f"{message.role.upper()}:\n{content}")
    return "\n\n".join(prompt_parts)


def make_chat_completion_response(
    text: str,
    model: str,
    request_id: str | None = None,
) -> dict:
    created = int(time.time())
    completion_id = request_id or f"chatcmpl-{uuid.uuid4().hex}"
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }


def make_chat_completion_chunk(
    completion_id: str,
    model: str,
    *,
    content: str | None = None,
    role: str | None = None,
    finish_reason: str | None = None,
) -> dict:
    delta: dict[str, str] = {}
    if role:
        delta["role"] = role
    if content:
        delta["content"] = content

    return {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }


def make_models_response(models: list[str] | list[dict]) -> dict:
    data: list[dict] = []
    for item in models:
        if isinstance(item, str):
            data.append(
                {
                    "id": item,
                    "object": "model",
                    "created": 0,
                    "owned_by": "cursor-cli-wrapper",
                }
            )
            continue

        data.append(
            {
                "id": item["id"],
                "object": "model",
                "created": 0,
                "owned_by": "cursor-cli-wrapper",
                **({"display_name": item["label"]} if item.get("label") else {}),
                **({"default": item["is_default"]} if "is_default" in item else {}),
                **({"current": item["is_current"]} if "is_current" in item else {}),
            }
        )

    return {
        "object": "list",
        "data": data,
    }


def make_error_response(message: str, error_type: str = "cursor_cli_error") -> dict:
    return {
        "error": {
            "message": message,
            "type": error_type,
        }
    }
