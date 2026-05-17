import asyncio
import json
from collections.abc import AsyncIterator

from fastapi.testclient import TestClient

from app.config import WRAPPER_RESPONSE_ACK_TEXT_CURSOR, WRAPPER_RESPONSE_GREETING_TEXT
from app.cursor_cli import CursorCLIAdapter
from app.main import app, get_agent_scheduler, get_settings
from helpers import FakeAgentScheduler, make_test_settings


class FakeStreamingCursorCLI(CursorCLIAdapter):
    def __init__(self, *, delay_before_first_delta: float = 0.0) -> None:
        super().__init__(make_test_settings())
        self._delay_before_first_delta = delay_before_first_delta

    async def stream_chat(self, prompt: str, model: str, **kwargs: object) -> AsyncIterator[str]:
        assert "USER:\nStream hello" in prompt
        assert model == "cursor-agent"
        if self._delay_before_first_delta > 0:
            await asyncio.sleep(self._delay_before_first_delta)
        for chunk in ("Hel", "lo"):
            yield chunk


def test_chat_completions_stream() -> None:
    fake_cli = FakeStreamingCursorCLI()
    app.dependency_overrides[get_settings] = lambda: make_test_settings()
    app.dependency_overrides[get_agent_scheduler] = lambda: FakeAgentScheduler(fake_cli)

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
    greeting_json = json.dumps(WRAPPER_RESPONSE_GREETING_TEXT, ensure_ascii=False)[1:-1]
    ack_json = json.dumps(WRAPPER_RESPONSE_ACK_TEXT_CURSOR, ensure_ascii=False)[1:-1]
    assert f'"content": "{greeting_json}"' in payload
    assert f'"content": "{ack_json}"' in payload
    greeting_pos = payload.index(f'"content": "{greeting_json}"')
    ack_pos = payload.index(f'"content": "{ack_json}"')
    first_hel_pos = payload.index('"content": "Hel"')
    lo_pos = payload.index('"content": "lo"')
    assert greeting_pos < ack_pos < first_hel_pos < lo_pos
    assert '"content": "Hel"' in payload
    assert '"content": "lo"' in payload
    assert "data: [DONE]" in payload


def test_chat_completions_stream_ack_idle_force() -> None:
    fake_cli = FakeStreamingCursorCLI(delay_before_first_delta=2.0)
    app.dependency_overrides[get_settings] = lambda: make_test_settings(
        response_ack_idle_force=True,
        response_ack_idle=1,
        stream_idle_seconds=0,
    )
    app.dependency_overrides[get_agent_scheduler] = lambda: FakeAgentScheduler(fake_cli)

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
    ack_json = json.dumps(WRAPPER_RESPONSE_ACK_TEXT_CURSOR, ensure_ascii=False)[1:-1]
    first_hel_pos = payload.index('"content": "Hel"')
    ack_pos = payload.index(f'"content": "{ack_json}"')
    assert ack_pos < first_hel_pos
