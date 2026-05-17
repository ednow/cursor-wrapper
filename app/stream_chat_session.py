"""``stream_chat`` 子进程句柄，供流式墙钟超时主动 terminate agent。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field

from .cli_base import AGENT_CURSOR, agent_cli_label

logger = logging.getLogger("cursor-wrapper")


@dataclass
class StreamChatSession:
    _process: asyncio.subprocess.Process | None = field(default=None, init=False)
    _stderr_task: asyncio.Task[bytes] | None = field(default=None, init=False)
    _terminated: bool = field(default=False, init=False)
    _agent: str = field(default=AGENT_CURSOR, init=False)

    def register(
        self,
        process: asyncio.subprocess.Process,
        stderr_task: asyncio.Task[bytes],
        *,
        agent: str = AGENT_CURSOR,
    ) -> None:
        self._process = process
        self._stderr_task = stderr_task
        self._agent = agent.strip().lower() or AGENT_CURSOR

    @property
    def agent(self) -> str:
        return self._agent

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    async def terminate_agent(self, *, reason: str = "timeout") -> None:
        if self._terminated:
            return
        self._terminated = True
        process = self._process
        if process is None or process.returncode is not None:
            return
        logger.warning(
            "%s terminate_agent agent=%s pid=%s reason=%r",
            agent_cli_label(self._agent),
            self._agent,
            process.pid,
            reason,
        )
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
        stderr_task = self._stderr_task
        if stderr_task is not None and not stderr_task.done():
            stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stderr_task
