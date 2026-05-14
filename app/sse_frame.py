"""Server-Sent Events 帧格式（与 OpenAI 流式 chat completions 对齐）。"""

from __future__ import annotations

import json


def sse_data(payload: dict | str) -> str:
    """一条 SSE ``data:`` 行，末尾两个换行表示事件结束。"""
    body = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return f"data: {body}\n\n"


def sse_comment(text: str) -> str:
    """SSE 注释行（客户端解析器通常忽略，用于 keep-alive）。"""
    safe = text.replace("\n", " ").replace("\r", " ")
    return f": {safe}\n\n"
