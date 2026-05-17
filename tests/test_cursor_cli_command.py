from app.cursor_cli import CursorCLIAdapter
from helpers import make_test_settings


def test_build_command_without_cursor_bin_keeps_agent_mode() -> None:
    settings = make_test_settings()
    adapter = CursorCLIAdapter(settings)

    command = adapter._build_command("hello", "auto", stream=False)

    assert "agent" not in command
    assert "--user-data-dir" not in command
    assert "--profile" not in command
