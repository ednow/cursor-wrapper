import asyncio
import json

from app.cursor_cli import CursorCLIAdapter


class ChunkedStream:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def read(self, _: int) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


def test_iter_ndjson_lines_handles_long_split_line() -> None:
    long_text = "x" * (CursorCLIAdapter._STDOUT_READ_CHUNK_SIZE + 1024)
    first = json.dumps({"type": "assistant", "text": long_text}).encode()
    second = json.dumps({"type": "result", "request_id": "req-123"}).encode()
    data = first + b"\n" + second + b"\n"
    stream = ChunkedStream([data[:100], data[100:70000], data[70000:]])

    async def collect() -> list[str]:
        return [line async for line in CursorCLIAdapter._iter_ndjson_lines(stream)]

    lines = asyncio.run(collect())

    assert len(lines) == 2
    assert json.loads(lines[0])["text"] == long_text
    assert json.loads(lines[1])["request_id"] == "req-123"


def test_iter_ndjson_lines_flushes_final_line_without_newline() -> None:
    final_line = json.dumps({"type": "result", "request_id": "req-456"}).encode()
    stream = ChunkedStream([final_line[:10], final_line[10:]])

    async def collect() -> list[str]:
        return [line async for line in CursorCLIAdapter._iter_ndjson_lines(stream)]

    lines = asyncio.run(collect())

    assert lines == [final_line.decode()]


def test_assistant_segment_delta_pure_increment_then_full_mirror() -> None:
    """复现 wrapper-2026-05-15.log 的现象：逐 token 增量 + 末尾完整镜像。"""
    parts = ["正在", "确认", "当前", "工作", "路径", "。", "正在确认当前工作路径。"]
    streamed = ""
    emitted: list[str] = []
    for text in parts:
        streamed, delta = CursorCLIAdapter._compute_assistant_segment_delta(streamed, text)
        if delta:
            emitted.append(delta)
    assert "".join(emitted) == "正在确认当前工作路径。"
    assert emitted == ["正在", "确认", "当前", "工作", "路径", "。"]


def test_assistant_segment_delta_full_mirror_only() -> None:
    """无逐 token 增量、只有一条完整镜像 delta 的情况，差量等于整段。"""
    streamed = ""
    streamed, delta = CursorCLIAdapter._compute_assistant_segment_delta(
        streamed, "Hello world."
    )
    assert delta == "Hello world."
    assert streamed == "Hello world."
    streamed, delta = CursorCLIAdapter._compute_assistant_segment_delta(
        streamed, "Hello world."
    )
    assert delta == ""
    assert streamed == "Hello world."


def test_assistant_segment_delta_mirror_extension() -> None:
    """镜像分多次扩展：``streamed`` 持续被扩展，只 yield 尾巴。"""
    streamed = ""
    deltas: list[str] = []
    for snapshot in ["Hel", "Hello", "Hello world", "Hello world."]:
        streamed, delta = CursorCLIAdapter._compute_assistant_segment_delta(
            streamed, snapshot
        )
        deltas.append(delta)
    assert deltas == ["Hel", "lo", " world", "."]
    assert streamed == "Hello world."


def test_assistant_segment_delta_tail_substring_mirror_is_skipped() -> None:
    """末尾子串镜像（兜底分支）应当被跳过，不重复 yield。"""
    streamed, _ = CursorCLIAdapter._compute_assistant_segment_delta("", "abc")
    streamed, delta = CursorCLIAdapter._compute_assistant_segment_delta(streamed, "bc")
    assert delta == ""
    assert streamed == "abc"


def test_assistant_segment_delta_segment_reset_between_tool_calls() -> None:
    """两段 assistant 中间出现 tool_call 时，调用方将 streamed 清零；两段独立累计互不影响。"""
    streamed = ""
    for piece in ["你好", "，世界", "你好，世界"]:
        streamed, _ = CursorCLIAdapter._compute_assistant_segment_delta(streamed, piece)
    assert streamed == "你好，世界"
    streamed = ""  # caller resets on tool_call
    deltas: list[str] = []
    for piece in ["再", "见", "再见"]:
        streamed, d = CursorCLIAdapter._compute_assistant_segment_delta(streamed, piece)
        if d:
            deltas.append(d)
    assert "".join(deltas) == "再见"
    assert streamed == "再见"
