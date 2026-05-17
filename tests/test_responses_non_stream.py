from fastapi.testclient import TestClient

from app.config import WRAPPER_RESPONSE_ACK_TEXT_CURSOR, WRAPPER_RESPONSE_GREETING_TEXT
from app.cursor_cli import CursorCLIAdapter, CursorCLIResult
from app.main import app, get_agent_scheduler, get_settings
from helpers import FakeAgentScheduler, make_test_settings


class FakeCursorCLI(CursorCLIAdapter):
    def __init__(self) -> None:
        super().__init__(make_test_settings())

    async def run_chat(self, prompt: str, model: str) -> CursorCLIResult:
        assert "USER:\nSay hi" in prompt
        assert model == "cursor-agent"
        return CursorCLIResult(text="hi", request_id="req-123")


def test_responses_non_stream_string_input() -> None:
    fake_cli = FakeCursorCLI()
    app.dependency_overrides[get_settings] = lambda: make_test_settings()
    app.dependency_overrides[get_agent_scheduler] = lambda: FakeAgentScheduler(fake_cli)

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
    expected = f"{WRAPPER_RESPONSE_GREETING_TEXT}{WRAPPER_RESPONSE_ACK_TEXT_CURSOR}hi"
    assert body["output_text"] == expected
    assert body["output"][0]["content"][0]["text"] == expected
