from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator, model_validator


class MessageContentPart(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: str = "text"
    text: str | None = None


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[MessageContentPart]
    tool_call_id: str | None = None
    name: str | None = None


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    model: str | None = None
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None


class ResponsesCreateRequest(BaseModel):
    """``POST /v1/responses`` 请求体（与 OpenAI Responses API 子集对齐）。"""

    model_config = ConfigDict(extra="ignore")

    model: str | None = None
    input: str | list[Any]
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None

    @model_validator(mode="after")
    def _validate_input_nonempty(self) -> ResponsesCreateRequest:
        if isinstance(self.input, str) and not self.input.strip():
            raise ValueError("input must be a non-empty string")
        if isinstance(self.input, list) and len(self.input) == 0:
            raise ValueError("input list must not be empty")
        return self


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
        if message.role == "tool":
            # Keep tool result context visible for the downstream CLI prompt.
            if message.name:
                role_label = f"TOOL[{message.name}]"
            elif message.tool_call_id:
                role_label = f"TOOL[{message.tool_call_id}]"
            else:
                role_label = "TOOL"
        else:
            role_label = message.role.upper()
        prompt_parts.append(f"{role_label}:\n{content}")
    return "\n\n".join(prompt_parts)


def _last_nonempty_user_message_index(messages: list[ChatMessage]) -> int | None:
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].role != "user":
            continue
        text = message_content_to_text(messages[i].content).strip()
        if text:
            return i
    return None


def build_cli_prompt(
    messages: list[ChatMessage],
    *,
    last_user_context_bridge: bool,
    persist_full_context: Callable[[str], str] | None = None,
) -> str:
    """组装传给 Cursor CLI 的 ``-p`` 文本。

    ``last_user_context_bridge`` 为 True 时：主问题只展开最后一条非空 user；
    若其前仍有可展平的上文，则将完整对话写入文件，并在文案中给出路径供按需读取。
    """
    if not last_user_context_bridge:
        return flatten_messages(messages)

    idx = _last_nonempty_user_message_index(messages)
    if idx is None:
        return flatten_messages(messages)

    last_text = message_content_to_text(messages[idx].content).strip()
    prior_flat = flatten_messages(messages[:idx]).strip()
    if not prior_flat:
        return (
            f"我的问题：{last_text}\n\n"
            "只有当我的问题不明确，或者你需要明确的上文信息时，你才需要查看额外上下文；"
            "当前请求未附带独立 prompt 文件。否则你可以直接回答问题。"
        )

    if persist_full_context is None:
        raise ValueError("persist_full_context is required when last_user_context_bridge needs a sidecar file")

    full_flat = flatten_messages(messages)
    path = persist_full_context(full_flat)
    return (
        f"我的问题：{last_text}\n\n"
        "只有当我的问题不明确，或者你需要明确的上文信息时，你才需要查看上下文并读取下列 prompt 文件中的完整对话；"
        "否则你可以直接回答问题。\n\n"
        f"Prompt file: {path}"
    )


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


def _coerce_responses_content_to_chat(content: Any) -> str | list[MessageContentPart]:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content) if content is not None else ""

    parts: list[MessageContentPart] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        bt = str(block.get("type") or "text")
        text = block.get("text")
        if text is None and isinstance(block.get("content"), str):
            text = block["content"]
        if text:
            parts.append(MessageContentPart(type="text", text=str(text)))
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0].text or ""
    return parts


def _responses_input_item_to_message(item: dict[str, Any]) -> ChatMessage | None:
    """将 Responses ``input`` 中的单条元素转为 :class:`ChatMessage`（无法识别则返回 ``None``）。"""
    t = item.get("type")

    if t == "function_call_output":
        call_id = item.get("call_id")
        output = item.get("output", "")
        if isinstance(output, (dict, list)):
            output = json.dumps(output, ensure_ascii=False)
        else:
            output = str(output or "")
        if not output.strip() and not call_id:
            return None
        return ChatMessage(role="tool", tool_call_id=str(call_id) if call_id else None, content=output)

    if t == "input_text":
        text = str(item.get("text") or "").strip()
        if not text:
            return None
        return ChatMessage(role="user", content=text)

    if t == "function_call":
        name = str(item.get("name") or "function")
        arguments = item.get("arguments", "")
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments, ensure_ascii=False)
        line = f"[function_call {name}] {arguments}".strip()
        return ChatMessage(role="assistant", content=line)

    role = item.get("role")
    if t == "message" or role in ("system", "user", "assistant", "tool"):
        if role not in ("system", "user", "assistant", "tool"):
            return None
        body = _coerce_responses_content_to_chat(item.get("content"))
        if role == "tool":
            if isinstance(body, str) and not body.strip():
                return None
            return ChatMessage(
                role="tool",
                content=body,
                tool_call_id=item.get("tool_call_id"),
                name=item.get("name"),
            )
        if isinstance(body, str) and not body.strip():
            return None
        return ChatMessage(role=role, content=body)

    return None


def responses_input_to_chat_messages(raw: str | list[Any]) -> list[ChatMessage]:
    """将 Responses API 的 ``input`` 转为内部 :class:`ChatMessage` 列表（供 ``build_cli_prompt`` 使用）。"""
    if isinstance(raw, str):
        return [ChatMessage(role="user", content=raw.strip())]

    out: list[ChatMessage] = []
    for elem in raw:
        if not isinstance(elem, dict):
            continue
        msg = _responses_input_item_to_message(elem)
        if msg is not None:
            out.append(msg)
    return out


def make_response_object(
    *,
    text: str,
    model: str,
    response_id: str | None = None,
    created_at: int | None = None,
) -> dict:
    """非流式 ``response`` 对象（与 OpenAI Responses 常见字段对齐）。"""
    rid = response_id or f"resp_{uuid.uuid4().hex}"
    ts = int(created_at if created_at is not None else time.time())
    msg_id = f"msg_{uuid.uuid4().hex}"
    return {
        "id": rid,
        "object": "response",
        "created_at": ts,
        "model": model,
        "status": "completed",
        "output": [
            {
                "type": "message",
                "id": msg_id,
                "role": "assistant",
                "status": "completed",
                "content": [
                    {
                        "type": "output_text",
                        "text": text,
                        "annotations": [],
                    }
                ],
            }
        ],
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        },
        "output_text": text,
    }


def make_responses_stream_created(*, response_id: str, model: str, created_at: int) -> dict:
    return {
        "type": "response.created",
        "response": {
            "id": response_id,
            "object": "response",
            "created_at": created_at,
            "model": model,
            "status": "in_progress",
        },
    }


def make_responses_output_item_added_message(*, item_id: str, output_index: int = 0) -> dict:
    return {
        "type": "response.output_item.added",
        "output_index": output_index,
        "item": {
            "type": "message",
            "id": item_id,
            "role": "assistant",
            "status": "in_progress",
        },
    }


def make_responses_content_part_added(
    *, item_id: str, output_index: int = 0, content_index: int = 0
) -> dict:
    """OpenClaw pi-ai 在 ``content`` 为空时会丢弃所有 ``output_text.delta``，必须先发此事件。"""
    return {
        "type": "response.content_part.added",
        "output_index": output_index,
        "item_id": item_id,
        "content_index": content_index,
        "part": {
            "type": "output_text",
            "text": "",
            "annotations": [],
        },
    }


def make_responses_output_text_delta(
    *, item_id: str, delta: str, output_index: int = 0, content_index: int = 0
) -> dict:
    return {
        "type": "response.output_text.delta",
        "output_index": output_index,
        "item_id": item_id,
        "content_index": content_index,
        "delta": delta,
    }


def make_responses_output_text_done(
    *, item_id: str, text: str, output_index: int = 0, content_index: int = 0
) -> dict:
    return {
        "type": "response.output_text.done",
        "output_index": output_index,
        "content_index": content_index,
        "item_id": item_id,
        "text": text,
    }


def greeting_output_item_events(
    *, item_id: str, text: str, output_index: int = 0
) -> list[dict]:
    """Responses 流：独立 greeting ``output_item`` 生命周期（added → content_part → delta → text.done → item.done）。"""
    return [
        make_responses_output_item_added_message(item_id=item_id, output_index=output_index),
        make_responses_content_part_added(item_id=item_id, output_index=output_index),
        make_responses_output_text_delta(item_id=item_id, delta=text, output_index=output_index),
        make_responses_output_text_done(item_id=item_id, text=text, output_index=output_index),
        make_responses_output_item_done_message(
            item_id=item_id,
            full_text=text,
            output_index=output_index,
        ),
    ]


def make_responses_output_item_done_message(
    *, item_id: str, full_text: str, output_index: int = 0
) -> dict:
    return {
        "type": "response.output_item.done",
        "output_index": output_index,
        "item": {
            "type": "message",
            "id": item_id,
            "role": "assistant",
            "status": "completed",
            "content": [
                {
                    "type": "output_text",
                    "text": full_text,
                    "annotations": [],
                }
            ],
        },
    }


def make_responses_stream_completed(
    *,
    response_id: str,
    model: str,
) -> dict:
    return {
        "type": "response.completed",
        "response": {
            "id": response_id,
            "object": "response",
            "status": "completed",
            "model": model,
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
            },
        },
    }


def make_responses_stream_error_event(message: str, *, code: str | None = None) -> dict:
    return {
        "type": "error",
        "error": {
            "type": "invalid_request_error",
            "message": message,
            "code": code,
            "param": None,
        },
    }
