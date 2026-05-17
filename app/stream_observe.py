"""流式 SSE 与 CLI 队列的消费阶段观测（仅打日志，不改变业务语义）。"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger("cursor-wrapper")


@dataclass
class StreamObserveContext:
    """记录单次流式请求在 queue / keepalive / SSE yield 各阶段的耗时与次数。"""

    stream_name: str
    stream_id: str
    keepalive_sec: float
    _t_start: float = field(default_factory=time.monotonic)
    _t_last_progress: float = field(default_factory=time.monotonic)
    _phase: str = "started"
    keepalive_count: int = 0
    n_queue_waits: int = 0

    def mark_progress(self, phase: str) -> None:
        self._phase = phase
        self._t_last_progress = time.monotonic()

    def idle_s(self) -> float:
        return time.monotonic() - self._t_last_progress

    def elapsed_s(self) -> float:
        return time.monotonic() - self._t_start

    def _prefix(self) -> str:
        return f"{self.stream_name} observe {self.stream_id}"

    def log_wait_queue_begin(self, *, prod_done: bool) -> None:
        self.n_queue_waits += 1
        logger.debug(
            "%s wait_queue begin wait #%d idle_s=%.1f elapsed_s=%.1f phase=%s "
            "keepalive_s=%.1f keepalive_n=%d prod_done=%s",
            self._prefix(),
            self.n_queue_waits,
            self.idle_s(),
            self.elapsed_s(),
            self._phase,
            self.keepalive_sec,
            self.keepalive_count,
            prod_done,
        )

    def log_queue_idle_timeout(self) -> None:
        logger.info(
            "%s wait_queue idle timeout (%.1fs) idle_s=%.1f elapsed_s=%.1f phase=%s",
            self._prefix(),
            self.keepalive_sec,
            self.idle_s(),
            self.elapsed_s(),
            self._phase,
        )

    def log_keepalive_emit_begin(self) -> None:
        logger.info(
            "%s keepalive emit begin keepalive_n=%d idle_s=%.1f elapsed_s=%.1f phase=%s",
            self._prefix(),
            self.keepalive_count + 1,
            self.idle_s(),
            self.elapsed_s(),
            self._phase,
        )

    def log_keepalive_emit_end(self, *, chunks: int, chars: int) -> None:
        self.keepalive_count += 1
        self.mark_progress("keepalive_emitted")
        logger.info(
            "%s keepalive emit end keepalive_n=%d chunks=%d chars=%d elapsed_s=%.1f",
            self._prefix(),
            self.keepalive_count,
            chunks,
            chars,
            self.elapsed_s(),
        )

    def log_queue_item(self, kind: str, *, payload_len: int | None = None) -> None:
        logger.debug(
            "%s wait_queue got kind=%s payload_len=%s idle_s=%.1f elapsed_s=%.1f phase=%s",
            self._prefix(),
            kind,
            payload_len,
            self.idle_s(),
            self.elapsed_s(),
            self._phase,
        )

    def log_sse_delta_begin(self, *, n: int, chunk_len: int) -> None:
        logger.debug(
            "%s sse_delta emit begin n=%d len=%d idle_s=%.1f elapsed_s=%.1f phase=%s",
            self._prefix(),
            n,
            chunk_len,
            self.idle_s(),
            self.elapsed_s(),
            self._phase,
        )

    def log_sse_delta_end(self, *, n: int) -> None:
        self.mark_progress("sse_delta_emitted")
        logger.debug(
            "%s sse_delta emit end n=%d elapsed_s=%.1f",
            self._prefix(),
            n,
            self.elapsed_s(),
        )

    def log_producer_put(self, kind: str, *, payload_len: int | None = None, qsize: int) -> None:
        logger.debug(
            "%s producer put kind=%s payload_len=%s qsize=%d elapsed_s=%.1f",
            self._prefix(),
            kind,
            payload_len,
            qsize,
            self.elapsed_s(),
        )

    def log_finally(self, *, prod_done: bool) -> None:
        logger.info(
            "%s generator finally prod_done=%s keepalive_n=%d elapsed_s=%.1f phase=%s",
            self._prefix(),
            prod_done,
            self.keepalive_count,
            self.elapsed_s(),
            self._phase,
        )
