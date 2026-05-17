from fastapi.testclient import TestClient

from app.cursor_cli import CursorCLIAdapter
from app.main import app, get_cursor_cli, get_settings
from helpers import make_test_settings


class FakeModelsCursorCLI(CursorCLIAdapter):
    def __init__(self) -> None:
        super().__init__(
            make_test_settings(model_aliases={"cursor-agent": "auto"}),
        )

    async def list_available_models(self) -> list[dict[str, str | bool]]:
        return [
            {
                "id": "auto",
                "label": "Auto",
                "is_default": False,
                "is_current": True,
            },
            {
                "id": "opus-4.6-thinking",
                "label": "Claude 4.6 Opus (Thinking)",
                "is_default": True,
                "is_current": False,
            },
        ]


def test_models_endpoint_uses_cli_models() -> None:
    app.dependency_overrides[get_settings] = lambda: make_test_settings(
        model_aliases={"cursor-agent": "auto"},
    )
    app.dependency_overrides[get_cursor_cli] = lambda: FakeModelsCursorCLI()

    client = TestClient(app)
    response = client.get("/v1/models")

    app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "list"
    assert body["data"][0]["id"] == "auto"
    assert body["data"][0]["display_name"] == "Auto"
    assert body["data"][0]["current"] is True
    assert body["data"][1]["id"] == "opus-4.6-thinking"
    assert body["data"][1]["default"] is True
