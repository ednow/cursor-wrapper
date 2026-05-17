from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

from .claude_cli import ClaudeCLIAdapter, ClaudeCLIResult
from .cli_base import CLIError, PromptFileMixin
from .config import Settings
from .cursor_cli import CursorCLIAdapter, CursorCLIResult
from .stream_chat_session import StreamChatSession

logger = logging.getLogger("cursor-wrapper")


class _StreamAdapter(Protocol):
    async def stream_chat(
        self,
        prompt: str,
        model: str,
        *,
        session: StreamChatSession | None = None,
    ) -> AsyncIterator[str]: ...

    async def run_chat(self, prompt: str, model: str) -> object: ...


@dataclass(frozen=True)
class AgentFallbackNotice:
    """某 agent 在产出首个 delta 前启动失败，需降级到下一个 agent。"""

    agent: str
    message: str


@dataclass(frozen=True)
class AgentRunResult:
    text: str
    agent: str
    request_id: str | None = None
    fallback_prefix: str = ""


def format_fallback_user_text(agent: str, message: str) -> str:
    return f"[{agent}] 启动失败: {message}\n\n"


class AgentScheduler(PromptFileMixin):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.settings = settings

    def get_adapter(self, agent: str) -> _StreamAdapter:
        name = agent.strip().lower()
        if name == "claude":
            return ClaudeCLIAdapter(self.settings)
        if name == "cursor":
            return CursorCLIAdapter(self.settings)
        raise ValueError(f"Unknown agent in schedule: {agent!r}")

    def cli_status_for_schedule(self) -> dict[str, object]:
        agents: dict[str, dict] = {}
        any_available = False
        for agent in self.settings.agent_schedule:
            try:
                status = self.get_adapter(agent).cli_status()  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                status = {"available": False, "error": str(exc)}
            agents[agent] = status
            if status.get("available"):
                any_available = True
        return {"agents": agents, "schedule": list(self.settings.agent_schedule), "any_available": any_available}

    async def stream_with_fallback(
        self,
        prompt: str,
        model: str,
        *,
        session: StreamChatSession | None = None,
    ) -> AsyncIterator[str | AgentFallbackNotice]:
        last_error: str | None = None
        schedule = self.settings.agent_schedule
        if not schedule:
            raise CLIError("Agent schedule is empty.")

        for index, agent in enumerate(schedule):
            adapter = self.get_adapter(agent)
            yielded = False
            try:
                logger.info("agent stream start agent=%s model=%r", agent, model)
                async for text in adapter.stream_chat(prompt, model, session=session):
                    yielded = True
                    yield text
                return
            except CLIError as exc:
                last_error = str(exc)
                if yielded:
                    raise
                logger.warning(
                    "agent stream startup failed agent=%s error=%s will_fallback=%s",
                    agent,
                    last_error,
                    index + 1 < len(schedule),
                )
                if index + 1 < len(schedule):
                    yield AgentFallbackNotice(agent=agent, message=last_error)
                    continue
                raise

        raise CLIError(last_error or "All agents failed to start.")

    async def run_with_fallback(self, prompt: str, model: str) -> AgentRunResult:
        fallback_parts: list[str] = []
        last_error: str | None = None
        schedule = self.settings.agent_schedule
        if not schedule:
            raise CLIError("Agent schedule is empty.")

        for index, agent in enumerate(schedule):
            adapter = self.get_adapter(agent)
            try:
                logger.info("agent run start agent=%s model=%r", agent, model)
                result = await adapter.run_chat(prompt, model)
                text = result.text
                if isinstance(result, (CursorCLIResult, ClaudeCLIResult)):
                    request_id = result.request_id
                else:
                    request_id = getattr(result, "request_id", None)
                prefix = "".join(fallback_parts)
                return AgentRunResult(
                    text=prefix + text,
                    agent=agent,
                    request_id=request_id,
                    fallback_prefix=prefix,
                )
            except CLIError as exc:
                last_error = str(exc)
                logger.warning(
                    "agent run startup failed agent=%s error=%s will_fallback=%s",
                    agent,
                    last_error,
                    index + 1 < len(schedule),
                )
                if index + 1 < len(schedule):
                    fallback_parts.append(format_fallback_user_text(agent, last_error))
                    continue
                raise

        raise CLIError(last_error or "All agents failed.")
