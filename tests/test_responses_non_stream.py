from fastapi.testclient import TestClient

from app.config import WRAPPER_RESPONSE_GREETING_TEXT, Settings
from app.cursor_cli import CursorCLIAdapter, CursorCLIResult
from app.main import app, get_cursor_cli, get_settings


class FakeCursorCLI(CursorCLIAdapter):
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

    async def run_chat(self, prompt: str, model: str) -> CursorCLIResult:
        assert "USER:\nSay hi" in prompt
        assert model == "cursor-agent"
        return CursorCLIResult(text="hi", request_id="req-123")


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


def test_responses_non_stream_string_input() -> None:
    app.dependency_overrides[get_settings] = _override_settings
    app.dependency_overrides[get_cursor_cli] = lambda: FakeCursorCLI()

    client = TestClient(app)
    response = client.post(
        "/v1/responses",
        json={"input": "Say hi"},
    )

    app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "response"
    assert body["model"] == "cursor-agent"
    assert body["status"] == "completed"
    assert body["id"] == "req-123"
    assert body["output_text"] == f"{WRAPPER_RESPONSE_GREETING_TEXT}hi"
    assert body["output"][0]["content"][0]["text"] == f"{WRAPPER_RESPONSE_GREETING_TEXT}hi"
