"""流式空闲超时：连续若干秒未向客户端推送 SSE 正文则终止 agent。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass

from .stream_chat_session import StreamChatSession

logger = logging.getLogger("cursor-wrapper")

STREAM_AGENT_KILLED_MESSAGE = "长时间未推送新内容，agent 已被终止。\n"

_SENSITIVE_HEADER_NAMES: frozenset[str] = frozenset(
    {
        "authorization",
        "cookie",
        "proxy-authorization",
        "x-api-key",
        "api-key",
    }
)

UPSTREAM_TIMEOUT_HEADER_NAMES: tuple[str, ...] = (
    "x-request-timeout",
    "x-request-timeout-seconds",
    "x-read-timeout",
    "x-read-timeout-seconds",
    "x-stream-timeout",
    "x-stream-timeout-seconds",
    "x-timeout",
    "x-timeout-seconds",
    "x-client-read-timeout",
    "x-client-read-timeout-seconds",
    "x-forwarded-timeout",
    "x-forwarded-read-timeout",
    "x-forwarded-request-timeout",
    "x-proxy-read-timeout",
    "x-proxy-request-timeout",
    "x-openai-read-timeout",
)


@dataclass(frozen=True)
class StreamIdleSecondsResolution:
    """连续无 SSE 正文推送的空闲上限解析结果。"""

    effective: int
    config_seconds: int
    margin_seconds: int
    upstream_header: str | None
    upstream_seconds: int | None
    upstream_cap_seconds: int | None


def _parse_timeout_header_value(raw: str) -> int | None:
    text = raw.strip()
    if not text:
        return None
    lower = text.lower()
    if lower.endswith("ms"):
        try:
            ms = int(text[:-2].strip())
        except ValueError:
            return None
        return max(1, ms // 1000) if ms > 0 else None
    if lower.endswith("s"):
        text = text[:-1].strip()
    try:
        seconds = int(float(text))
    except ValueError:
        return None
    return seconds if seconds > 0 else None


def _redact_header_value(key: str, value: str) -> str:
    if key.lower() in _SENSITIVE_HEADER_NAMES:
        return "<redacted>"
    if len(value) > 240:
        return value[:240] + "..."
    return value


def _debug_log_incoming_headers(stream_id: str, headers: Mapping[str, str]) -> None:
    if not headers:
        logger.debug("stream request headers stream_id=%s: (empty)", stream_id or "(unknown)")
        return

    header_parts = [
        f"{key}={_redact_header_value(key, value)!r}"
        for key, value in sorted(headers.items(), key=lambda item: item[0].lower())
    ]
    logger.debug(
        "stream request headers stream_id=%s count=%d:\n  %s",
        stream_id or "(unknown)",
        len(headers),
        "\n  ".join(header_parts),
    )

    lowered = {k.lower(): (k, v) for k, v in headers.items()}
    present_timeout_names = [
        f"{lowered[name][0]}={lowered[name][1]!r}"
        for name in UPSTREAM_TIMEOUT_HEADER_NAMES
        if name in lowered
    ]
    if present_timeout_names:
        logger.debug(
            "stream timeout header names present stream_id=%s: %s",
            stream_id or "(unknown)",
            "; ".join(present_timeout_names),
        )
    else:
        logger.debug(
            "stream timeout header names present stream_id=%s: (none of %s)",
            stream_id or "(unknown)",
            ", ".join(UPSTREAM_TIMEOUT_HEADER_NAMES),
        )


def _find_upstream_timeout_seconds(headers: Mapping[str, str]) -> tuple[str | None, int | None]:
    lowered = {k.lower(): (k, v) for k, v in headers.items()}
    for name in UPSTREAM_TIMEOUT_HEADER_NAMES:
        entry = lowered.get(name)
        if entry is None:
            continue
        original_key, raw_value = entry
        seconds = _parse_timeout_header_value(raw_value)
        if seconds is not None:
            return original_key, seconds
    return None, None


def resolve_effective_stream_idle_seconds(
    config_seconds: int,
    headers: Mapping[str, str],
    *,
    margin_seconds: int = 10,
    stream_id: str = "",
) -> StreamIdleSecondsResolution:
    """``effective = min(config, upstream - margin)``，表示允许连续多久不向客户端推 SSE 正文。"""
    _debug_log_incoming_headers(stream_id, headers)
    upstream_header, upstream_seconds = _find_upstream_timeout_seconds(headers)

    upstream_cap: int | None = None
    if upstream_seconds is not None:
        cap = upstream_seconds - margin_seconds
        if cap > 0:
            upstream_cap = cap
        else:
            logger.info(
                "stream idle_seconds upstream cap skipped stream_id=%s header=%r upstream_s=%s "
                "margin_s=%s (upstream - margin <= 0)",
                stream_id or "(unknown)",
                upstream_header,
                upstream_seconds,
                margin_seconds,
            )

    candidates: list[int] = []
    if config_seconds > 0:
        candidates.append(config_seconds)
    if upstream_cap is not None:
        candidates.append(upstream_cap)

    effective = min(candidates) if candidates else 0

    logger.info(
        "stream idle_seconds resolved stream_id=%s config_s=%s upstream_header=%s "
        "upstream_s=%s upstream_cap_s=%s margin_s=%s effective_s=%s",
        stream_id or "(unknown)",
        config_seconds,
        upstream_header if upstream_header is not None else "(none)",
        upstream_seconds if upstream_seconds is not None else "(none)",
        upstream_cap if upstream_cap is not None else "(none)",
        margin_seconds,
        effective if effective > 0 else "(disabled)",
    )

    return StreamIdleSecondsResolution(
        effective=effective,
        config_seconds=config_seconds,
        margin_seconds=margin_seconds,
        upstream_header=upstream_header,
        upstream_seconds=upstream_seconds,
        upstream_cap_seconds=upstream_cap,
    )


# 兼容旧名
resolve_effective_stream_max_seconds = resolve_effective_stream_idle_seconds
StreamMaxSecondsResolution = StreamIdleSecondsResolution


def content_idle_exceeded(last_content_push_at: float, idle_seconds: int) -> bool:
    """距上次向客户端写出 SSE（正文或 keepalive 注释）是否已超过 ``idle_seconds``。"""
    return idle_seconds > 0 and (time.monotonic() - last_content_push_at) >= idle_seconds


def queue_wait_seconds_for_content_idle(
    keepalive_sec: float,
    last_content_push_at: float,
    idle_seconds: int,
) -> float:
    """下一次 ``queue.get`` 等待时长：在 keepalive 间隔与剩余空闲时间之间取较小值。"""
    if idle_seconds <= 0:
        return keepalive_sec
    remaining = idle_seconds - (time.monotonic() - last_content_push_at)
    if remaining <= 0:
        return 0.0
    return min(keepalive_sec, remaining)


def response_ack_idle_force_remaining_seconds(
    *,
    force_enabled: bool,
    idle_seconds: int,
    wait_started_at: float,
    ack_emitted: bool,
    cursor_content_started: bool,
) -> float | None:
    """流式强制 ack：返回距触发还剩多少秒；None 表示未启用该计时。"""
    if (
        not force_enabled
        or ack_emitted
        or cursor_content_started
        or idle_seconds <= 0
    ):
        return None
    return max(0.0, idle_seconds - (time.monotonic() - wait_started_at))


async def kill_stream_cli_session(
    session: StreamChatSession,
    prod: asyncio.Task[object],
    *,
    stream_id: str,
    idle_seconds: int,
) -> None:
    logger.warning(
        "stream content-idle timeout stream_id=%s agent=%s idle_seconds=%s pid=%s",
        stream_id,
        session.agent,
        idle_seconds,
        session.pid,
    )
    await session.terminate_agent(reason=f"content_idle_seconds={idle_seconds}")
    if not prod.done():
        prod.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await prod
