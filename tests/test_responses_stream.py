import json
from collections.abc import AsyncIterator

from fastapi.testclient import TestClient

from app.config import WRAPPER_RESPONSE_ACK_TEXT_CURSOR, WRAPPER_RESPONSE_GREETING_TEXT
from app.cursor_cli import CursorCLIAdapter
from app.main import app, get_agent_scheduler, get_settings
from helpers import FakeAgentScheduler, make_test_settings


class FakeStreamingCursorCLI(CursorCLIAdapter):
    def __init__(self) -> None:
        super().__init__(make_test_settings())

    async def stream_chat(self, prompt: str, model: str, **kwargs: object) -> AsyncIterator[str]:
        assert "USER:\nStream hello" in prompt
        assert model == "cursor-agent"
        for chunk in ("Hel", "lo"):
            yield chunk


def test_responses_stream_list_input() -> None:
    fake_cli = FakeStreamingCursorCLI()
    app.dependency_overrides[get_settings] = lambda: make_test_settings()
    app.dependency_overrides[get_agent_scheduler] = lambda: FakeAgentScheduler(fake_cli)

    client = TestClient(app)
    with client.stream(
        "POST",
        "/v1/responses",
        json={
            "input": [
                {"type": "message", "role": "user", "content": "Stream hello"},
            ],
            "stream": True,
        },
    ) as response:
        payload = "".join(response.iter_text())

    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert "data:" in payload
    assert '"type": "response.created"' in payload
    assert '"type": "response.output_text.delta"' in payload
    greeting_json = json.dumps(WRAPPER_RESPONSE_GREETING_TEXT, ensure_ascii=False)[1:-1]
    ack_json = json.dumps(WRAPPER_RESPONSE_ACK_TEXT_CURSOR, ensure_ascii=False)[1:-1]
    assert f'"delta": "{greeting_json}"' in payload
    assert f'"delta": "{ack_json}"' in payload
    assert '"type": "response.output_text.done"' in payload
    assert f'"text": "{greeting_json}"' in payload
    assert payload.count('"type": "response.output_item.added"') == 3
    assert payload.count('"type": "response.content_part.added"') == 3
    assert payload.count('"type": "response.output_item.done"') == 3
    greeting_item_done_pos = payload.index('"type": "response.output_item.done"')
    ack_text_done_pos = payload.index(f'"text": "{ack_json}"')
    first_hel_delta_pos = payload.index('"delta": "Hel"')
    lo_delta_pos = payload.index('"delta": "lo"')
    assert greeting_item_done_pos < ack_text_done_pos < first_hel_delta_pos < lo_delta_pos
    assert '"delta": "Hel"' in payload
    assert '"delta": "lo"' in payload
    assert '"type": "response.completed"' in payload
