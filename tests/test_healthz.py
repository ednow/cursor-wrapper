from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app, get_settings


def _override_settings() -> Settings:
    return Settings(
        cursor_bin="missing-agent",
        cursor_workspace=".",
        wrapper_api_key=None,
        default_model="cursor-agent",
        model_aliases={},
        trust_workspace=True,
        approve_mcps=False,
        force=False,
        sandbox=None,
    )


def test_healthz_reports_missing_cursor_cli() -> None:
    app.dependency_overrides[get_settings] = _override_settings

    client = TestClient(app)
    response = client.get("/healthz")

    app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["cursor_cli"]["available"] is False
    assert body["cursor_cli"]["configured_bin"] == "missing-agent"
