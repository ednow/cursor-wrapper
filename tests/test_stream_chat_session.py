from app.cli_base import AGENT_CLAUDE, AGENT_CURSOR
from app.stream_chat_session import StreamChatSession


def test_stream_chat_session_default_agent() -> None:
    session = StreamChatSession()
    assert session.agent == AGENT_CURSOR


def test_stream_chat_session_register_claude() -> None:
    session = StreamChatSession()

    class _Proc:
        pid = 12345
        returncode = None

    session.register(_Proc(), _FakeStderrTask(), agent=AGENT_CLAUDE)  # type: ignore[arg-type]
    assert session.agent == AGENT_CLAUDE


class _FakeStderrTask:
    def done(self) -> bool:
        return True
