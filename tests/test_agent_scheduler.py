import pytest

from app.agent_scheduler import AgentFallbackNotice, AgentScheduler, format_fallback_user_text
from app.cli_base import CLIError
from app.config import Settings
from app.cursor_cli import CursorCLIAdapter


class _FailingThenOkCLI(CursorCLIAdapter):
    def __init__(self, settings: Settings, *, fail_first: bool) -> None:
        super().__init__(settings)
        self._fail_first = fail_first
        self._started = False

    async def stream_chat(self, prompt: str, model: str, **kwargs: object):  # noqa: ANN201
        if self._fail_first and not self._started:
            self._started = True
            raise CLIError("claude not available")
        yield "ok"

    async def run_chat(self, prompt: str, model: str):  # noqa: ANN201
        if self._fail_first:
            raise CLIError("claude not available")
        from app.cursor_cli import CursorCLIResult

        return CursorCLIResult(text="done")


def _settings(schedule: tuple[str, ...]) -> Settings:
    return Settings(
        cursor_bin="agent",
        claude_bin="claude",
        agent_schedule=schedule,
        cursor_workspace=".",
        wrapper_api_key=None,
        default_model="cursor-agent",
        model_aliases={},
        trust_workspace=True,
        approve_mcps=False,
        force=False,
        sandbox=None,
    )


@pytest.mark.asyncio
async def test_stream_with_fallback_emits_notice_then_text(monkeypatch) -> None:
    settings = _settings(("claude", "cursor"))
    scheduler = AgentScheduler(settings)

    def fake_get(agent: str) -> CursorCLIAdapter:
        if agent == "claude":
            return _FailingThenOkCLI(settings, fail_first=True)
        return _FailingThenOkCLI(settings, fail_first=False)

    monkeypatch.setattr(scheduler, "get_adapter", fake_get)

    items: list[object] = []
    async for item in scheduler.stream_with_fallback("hi", "cursor-agent"):
        items.append(item)

    assert len(items) == 2
    assert isinstance(items[0], AgentFallbackNotice)
    assert items[0].agent == "claude"
    assert "claude not available" in items[0].message
    assert items[1] == "ok"


@pytest.mark.asyncio
async def test_run_with_fallback_prefixes_failed_agent_message(monkeypatch) -> None:
    settings = _settings(("claude", "cursor"))
    scheduler = AgentScheduler(settings)

    def fake_get(agent: str) -> CursorCLIAdapter:
        if agent == "claude":
            return _FailingThenOkCLI(settings, fail_first=True)
        return _FailingThenOkCLI(settings, fail_first=False)

    monkeypatch.setattr(scheduler, "get_adapter", fake_get)

    result = await scheduler.run_with_fallback("hi", "cursor-agent")
    assert result.agent == "cursor"
    assert result.text.startswith(format_fallback_user_text("claude", "claude not available"))
    assert result.text.endswith("done")


def test_format_fallback_user_text() -> None:
    text = format_fallback_user_text("claude", "boom")
    assert "[claude]" in text
    assert "boom" in text
