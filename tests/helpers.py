from app.agent_scheduler import AgentRunResult, AgentScheduler
from app.config import Settings
from app.cursor_cli import CursorCLIAdapter


class FakeAgentScheduler(AgentScheduler):
    """单测用：固定走注入的 CursorCLIAdapter，不触发真实 claude/cursor 子进程。"""

    def __init__(self, cli: CursorCLIAdapter) -> None:
        super().__init__(cli.settings)
        self._cli = cli

    async def stream_with_fallback(self, prompt, model, *, session=None):  # noqa: ANN001, ANN201
        async for text in self._cli.stream_chat(prompt, model, session=session):
            yield text

    async def run_with_fallback(self, prompt, model):  # noqa: ANN001, ANN201
        result = await self._cli.run_chat(prompt, model)
        return AgentRunResult(
            text=result.text,
            agent="cursor",
            request_id=result.request_id,
        )

    def persist_full_prompt_for_bridge(self, content: str) -> str:
        return self._cli.persist_full_prompt_for_bridge(content)


def make_test_settings(**overrides: object) -> Settings:
    """构造测试用 Settings；默认仅调度 cursor，避免单测误调真实 claude。"""
    base = dict(
        cursor_bin="agent",
        claude_bin="claude",
        agent_schedule=("cursor",),
        cursor_workspace=".",
        wrapper_api_key=None,
        default_model="cursor-agent",
        model_aliases={},
        trust_workspace=True,
        approve_mcps=False,
        force=False,
        sandbox=None,
    )
    base.update(overrides)
    return Settings(**base)
