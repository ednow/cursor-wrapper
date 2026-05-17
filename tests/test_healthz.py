from fastapi.testclient import TestClient

from app.main import app, get_settings
from helpers import make_test_settings


def test_healthz_reports_missing_cursor_cli() -> None:
    app.dependency_overrides[get_settings] = lambda: make_test_settings(
        cursor_bin="missing-agent",
        claude_bin="missing-claude",
        agent_schedule=("cursor", "claude"),
    )

    client = TestClient(app)
    response = client.get("/healthz")

    app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["cursor_cli"]["available"] is False
    assert body["cursor_cli"]["configured_bin"] == "missing-agent"
