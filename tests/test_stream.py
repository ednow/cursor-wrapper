from collections.abc import AsyncIterator

from fastapi.testclient import TestClient

from app.config import Settings
from app.cursor_cli import CursorCLIAdapter
from app.main import app, get_cursor_cli, get_settings


class FakeStreamingCursorCLI(CursorCLIAdapter):
    def __init__(self) -> None:
        super().__init__(
            Settings(
                cursor_bin="agent",
                cursor_workspace=".",
                wrapper_api_key=None,
                default_model="cursor-agent",
                model_aliases={},
                trust_workspace=True,
                approve_mcps=False,
                force=False,
                sandbox=None,
            )
        )

    async def stream_chat(self, prompt: str, model: str) -> AsyncIterator[str]:
        assert "USER:\nStream hello" in prompt
        assert model == "cursor-agent"
        for chunk in ("Hel", "lo"):
            yield chunk


def _override_settings() -> Settings:
    return Settings(
        cursor_bin="agent",
        cursor_workspace=".",
        wrapper_api_key=None,
        default_model="cursor-agent",
        model_aliases={},
        trust_workspace=True,
        approve_mcps=False,
        force=False,
        sandbox=None,
    )


def test_chat_completions_stream() -> None:
    app.dependency_overrides[get_settings] = _override_settings
    app.dependency_overrides[get_cursor_cli] = lambda: FakeStreamingCursorCLI()

    client = TestClient(app)
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "messages": [
                {"role": "user", "content": "Stream hello"},
            ],
            "stream": True,
        },
    ) as response:
        payload = "".join(response.iter_text())

    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert "data:" in payload
    assert '"role": "assistant"' in payload
    assert '"content": "Hel"' in payload
    assert '"content": "lo"' in payload
    assert "data: [DONE]" in payload
