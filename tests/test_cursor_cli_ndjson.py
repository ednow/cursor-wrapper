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
