"""从 wrapper 日志解析指定 completion_id 的全部 stream chunk，生成 mock_startup_outputs.json。"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path


def _parse_ts(line: str) -> datetime | None:
    m = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3})", line)
    if not m:
        return None
    return datetime.strptime(m.group(1) + m.group(2), "%Y-%m-%d %H:%M:%S%f")


def _started_ts_from_text(text: str, completion_id: str) -> datetime | None:
    needle = f"stream started completion_id={completion_id}"
    for line in text.splitlines():
        if needle in line:
            return _parse_ts(line)
    return None


def _done_ts_from_text(text: str, completion_id: str) -> datetime | None:
    """``stream done`` 行（``app.main`` 里 ``stop`` + ``[DONE]`` 输出后写的）的时间戳。"""
    needle = f"stream done completion_id={completion_id}"
    for line in text.splitlines():
        if needle in line:
            return _parse_ts(line)
    return None


def build_steps(
    log_path: Path, completion_id: str
) -> tuple[datetime, list[dict[str, float | str | int]], int, int, float]:
    """合并 ``stream keepalive`` 与 ``stream chunk``，按时间戳排序生成 steps。

    ``stream chunk`` 的 ``body=`` 之后可能跨多行物理行；必须以 ``len=`` 声明的**字符数**
    （与 ``app.main`` 中 ``len(payload)`` 一致，Python ``str`` 码点长度）从日志原文中截取，
    否则会把 ``\\n``、续行的 ``> `` 等截断，导致回放与真实 SSE 不一致。

    返回 tail_silence_s = ``stream done`` 时间 − 最后一条 chunk 时间；这段静默对应
    ``app.main`` 的 ``finally``/``_reap_after_result_event`` 的尾巴，会影响下游
    （例如 OpenClaw → TT）按"流空闲"分批的切分点。
    """
    text = log_path.read_text(encoding="utf-8", errors="replace")
    started_ts = _started_ts_from_text(text, completion_id)
    done_ts = _done_ts_from_text(text, completion_id)

    esc = re.escape(completion_id)
    chunk_header_re = re.compile(
        r"(?m)^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3}).*?stream chunk completion_id="
        + esc
        + r" n=(\d+) len=(\d+) body=",
    )
    keepalive_re = re.compile(
        r"(?m)^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3}).*?stream keepalive completion_id="
        + esc
        + r" chunks=(\d+) chars=(\d+)",
    )

    # (timestamp, file_offset, kind, payload)  payload: ("ka", ch, chh) | ("ch", n, ln, body)
    events: list[tuple[datetime, int, str, tuple]] = []

    for m in chunk_header_re.finditer(text):
        ts = datetime.strptime(m.group(1) + m.group(2), "%Y-%m-%d %H:%M:%S%f")
        n = int(m.group(3))
        ln = int(m.group(4))
        start = m.end()
        body = text[start : start + ln]
        if len(body) != ln:
            raise ValueError(
                f"stream chunk n={n} len={ln} 与日志可截取长度不一致："
                f"offset={start} 实际可读 {len(body)}（文件可能被截断或 completion_id 匹配错误）"
            )
        events.append((ts, m.start(), "chunk", (n, ln, body)))

    for m in keepalive_re.finditer(text):
        ts = datetime.strptime(m.group(1) + m.group(2), "%Y-%m-%d %H:%M:%S%f")
        ch, chh = int(m.group(3)), int(m.group(4))
        events.append((ts, m.start(), "keepalive", (ch, chh)))

    if started_ts is None:
        raise ValueError("未找到 stream started 行")
    if not events:
        raise ValueError("未找到任何 stream keepalive 或 stream chunk 行")

    events.sort(key=lambda e: (e[0], e[1]))

    steps: list[dict[str, float | str | int]] = []
    prev = started_ts
    n_keepalive = 0
    n_chunk = 0
    for ts, _pos, kind, payload in events:
        delay = max(0.0, (ts - prev).total_seconds())
        if kind == "keepalive":
            ch, chh = payload[0], payload[1]
            steps.append(
                {
                    "event": "keepalive",
                    "delay_before_s": round(delay, 6),
                    "chunks": ch,
                    "chars": chh,
                }
            )
            n_keepalive += 1
        else:
            n, ln, body = payload[0], payload[1], payload[2]
            steps.append(
                {
                    "event": "chunk",
                    "delay_before_s": round(delay, 6),
                    "content": f"n={n} len={ln} body={body}",
                }
            )
            n_chunk += 1
        prev = ts

    tail_silence_s = 0.0
    if done_ts is not None:
        last_ts = events[-1][0]
        tail_silence_s = max(0.0, round((done_ts - last_ts).total_seconds(), 6))

    return started_ts, steps, n_keepalive, n_chunk, tail_silence_s


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--log", type=Path, default=Path("logs/wrapper-2026-05-13.log"))
    p.add_argument(
        "--completion-id",
        default="chatcmpl-d9b48334a2b54079b33bc98d34637b9f",
        help="日志中的 completion_id",
    )
    p.add_argument("--out", type=Path, default=Path("mock/mock_startup_outputs.json"))
    args = p.parse_args()

    started_ts, steps, n_keepalive, n_chunk, tail_silence_s = build_steps(
        args.log, args.completion_id
    )
    doc = {
        "version": 1,
        "title": f"全量 stream keepalive + chunk 复现（{args.completion_id}）",
        "description": (
            "由日志解析该 completion 的 stream keepalive（chars=0 等）与 stream chunk，按时间戳合并排序；"
            "delay_before_s 为相对上一事件（含 keepalive）的时间差。keepalive 步无 content，使用 chunks/chars。"
            "chunk 的 body 按 len= 从日志原文精确截取（可跨物理行），与真实 SSE 字节一致。"
            "tail_silence_before_done_s 记录最后一条 chunk → stream done 的静默（含 _reap_after_result_event 等），"
            "回放需在最后一个 chunk 后等待该时长再发 stop/[DONE]，否则下游按流空闲分段的策略会被打乱。"
        ),
        "source": {
            "log": str(args.log).replace("\\", "/"),
            "completion_id": args.completion_id,
            "stream_started_at": started_ts.isoformat(sep=" "),
            "step_count": len(steps),
            "keepalive_count": n_keepalive,
            "chunk_count": n_chunk,
            "tail_silence_before_done_s": tail_silence_s,
        },
        "tail_silence_before_done_s": tail_silence_s,
        "steps": steps,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"wrote {args.out} steps={len(steps)} keepalive={n_keepalive} "
        f"chunk={n_chunk} tail_silence={tail_silence_s:.3f}s",
    )


if __name__ == "__main__":
    main()
