from app.config import Settings
from app.cursor_cli import CursorCLIAdapter


def test_build_command_without_cursor_bin_keeps_agent_mode() -> None:
    settings = Settings(
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
    adapter = CursorCLIAdapter(settings)

    command = adapter._build_command("hello", "auto", stream=False)

    assert "agent" not in command
    assert "--user-data-dir" not in command
    assert "--profile" not in command
