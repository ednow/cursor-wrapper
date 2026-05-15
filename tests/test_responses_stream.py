import json
from collections.abc import AsyncIterator

from fastapi.testclient import TestClient

from app.config import WRAPPER_RESPONSE_GREETING_TEXT, Settings
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


def test_responses_stream_list_input() -> None:
    app.dependency_overrides[get_settings] = _override_settings
    app.dependency_overrides[get_cursor_cli] = lambda: FakeStreamingCursorCLI()

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
    assert f'"delta": "{greeting_json}"' in payload
    assert '"type": "response.output_text.done"' in payload
    assert f'"text": "{greeting_json}"' in payload
    assert payload.count('"type": "response.output_item.added"') == 2
    assert payload.count('"type": "response.output_item.done"') == 2
    greeting_item_done_pos = payload.index('"type": "response.output_item.done"')
    body_item_added_pos = payload.rindex('"type": "response.output_item.added"')
    hel_delta_pos = payload.index('"delta": "Hel"')
    assert greeting_item_done_pos < body_item_added_pos < hel_delta_pos
    assert '"delta": "Hel"' in payload
    assert '"delta": "lo"' in payload
    assert '"type": "response.completed"' in payload
