import app.config as config_module


def test_config_uses_env_when_inline_value_is_empty(monkeypatch) -> None:
    monkeypatch.setenv("WRAPPER_API_KEY", "env-key")
    monkeypatch.setattr(config_module, "CONFIG_WRAPPER_API_KEY", "")

    settings = config_module.Settings.from_env()

    assert settings.wrapper_api_key == "env-key"


def test_config_prefers_inline_value_when_present(monkeypatch) -> None:
    monkeypatch.setenv("WRAPPER_API_KEY", "env-key")
    monkeypatch.setattr(config_module, "CONFIG_WRAPPER_API_KEY", "inline-key")

    settings = config_module.Settings.from_env()

    assert settings.wrapper_api_key == "inline-key"


def test_config_cli_last_user_context_bridge_from_env(monkeypatch) -> None:
    monkeypatch.setenv("CURSOR_CLI_LAST_USER_CONTEXT_BRIDGE", "true")
    monkeypatch.setattr(config_module, "CONFIG_CLI_LAST_USER_CONTEXT_BRIDGE", "")

    settings = config_module.Settings.from_env()

    assert settings.cli_last_user_context_bridge is True


def test_config_response_greeting_default_on(monkeypatch) -> None:
    monkeypatch.delenv("WRAPPER_RESPONSE_GREETING", raising=False)
    monkeypatch.setattr(config_module, "CONFIG_WRAPPER_RESPONSE_GREETING", "")

    settings = config_module.Settings.from_env()

    assert settings.response_greeting is True


def test_config_response_greeting_from_env(monkeypatch) -> None:
    monkeypatch.setenv("WRAPPER_RESPONSE_GREETING", "false")
    monkeypatch.setattr(config_module, "CONFIG_WRAPPER_RESPONSE_GREETING", "")

    settings = config_module.Settings.from_env()

    assert settings.response_greeting is False


def test_config_response_ack_default_on(monkeypatch) -> None:
    monkeypatch.delenv("WRAPPER_RESPONSE_ACK", raising=False)
    monkeypatch.setattr(config_module, "CONFIG_WRAPPER_RESPONSE_ACK", "")

    settings = config_module.Settings.from_env()

    assert settings.response_ack is True


def test_config_response_ack_from_env(monkeypatch) -> None:
    monkeypatch.setenv("WRAPPER_RESPONSE_ACK", "false")
    monkeypatch.setattr(config_module, "CONFIG_WRAPPER_RESPONSE_ACK", "")

    settings = config_module.Settings.from_env()

    assert settings.response_ack is False


def test_config_response_ack_idle_force_default_off(monkeypatch) -> None:
    monkeypatch.delenv("WRAPPER_RESPONSE_ACK_IDLE_FORCE", raising=False)
    monkeypatch.delenv("WRAPPER_RESPONSE_ACK_IDLE", raising=False)
    monkeypatch.setattr(config_module, "CONFIG_WRAPPER_RESPONSE_ACK_IDLE_FORCE", "")
    monkeypatch.setattr(config_module, "CONFIG_WRAPPER_RESPONSE_ACK_IDLE", "")

    settings = config_module.Settings.from_env()

    assert settings.response_ack_idle_force is False
    assert settings.response_ack_idle == 30


def test_config_response_ack_idle_force_from_env(monkeypatch) -> None:
    monkeypatch.setenv("WRAPPER_RESPONSE_ACK_IDLE_FORCE", "true")
    monkeypatch.setenv("WRAPPER_RESPONSE_ACK_IDLE", "15")
    monkeypatch.setattr(config_module, "CONFIG_WRAPPER_RESPONSE_ACK_IDLE_FORCE", "")
    monkeypatch.setattr(config_module, "CONFIG_WRAPPER_RESPONSE_ACK_IDLE", "")

    settings = config_module.Settings.from_env()

    assert settings.response_ack_idle_force is True
    assert settings.response_ack_idle == 15


def test_config_response_ack_idle_inline_int(monkeypatch) -> None:
    monkeypatch.delenv("WRAPPER_RESPONSE_ACK_IDLE", raising=False)
    monkeypatch.setattr(config_module, "CONFIG_WRAPPER_RESPONSE_ACK_IDLE", 5)

    settings = config_module.Settings.from_env()

    assert settings.response_ack_idle == 5


def test_config_response_ack_idle_inline_int_overrides_env(monkeypatch) -> None:
    monkeypatch.setenv("WRAPPER_RESPONSE_ACK_IDLE", "99")
    monkeypatch.setattr(config_module, "CONFIG_WRAPPER_RESPONSE_ACK_IDLE", 5)

    settings = config_module.Settings.from_env()

    assert settings.response_ack_idle == 5


def test_config_approve_mcps_default_on(monkeypatch) -> None:
    monkeypatch.delenv("CURSOR_APPROVE_MCPS", raising=False)
    monkeypatch.setattr(config_module, "CONFIG_CURSOR_APPROVE_MCPS", "")

    settings = config_module.Settings.from_env()

    assert settings.approve_mcps is True


def test_config_approve_mcps_inline_bool_true(monkeypatch) -> None:
    monkeypatch.delenv("CURSOR_APPROVE_MCPS", raising=False)
    monkeypatch.setattr(config_module, "CONFIG_CURSOR_APPROVE_MCPS", True)

    settings = config_module.Settings.from_env()

    assert settings.approve_mcps is True


def test_config_approve_mcps_inline_bool_false(monkeypatch) -> None:
    monkeypatch.setenv("CURSOR_APPROVE_MCPS", "true")
    monkeypatch.setattr(config_module, "CONFIG_CURSOR_APPROVE_MCPS", False)

    settings = config_module.Settings.from_env()

    assert settings.approve_mcps is False


def test_config_stream_idle_seconds_default_120(monkeypatch) -> None:
    monkeypatch.delenv("CURSOR_STREAM_IDLE_SECONDS", raising=False)
    monkeypatch.delenv("CURSOR_STREAM_MAX_SECONDS", raising=False)
    monkeypatch.setattr(config_module, "CONFIG_CURSOR_STREAM_IDLE_SECONDS", "")
    monkeypatch.setattr(config_module, "CONFIG_CURSOR_STREAM_MAX_SECONDS", "")

    settings = config_module.Settings.from_env()

    assert settings.stream_idle_seconds == 120


def test_config_stream_idle_seconds_legacy_max_name(monkeypatch) -> None:
    monkeypatch.delenv("CURSOR_STREAM_IDLE_SECONDS", raising=False)
    monkeypatch.setattr(config_module, "CONFIG_CURSOR_STREAM_IDLE_SECONDS", "")
    monkeypatch.setattr(config_module, "CONFIG_CURSOR_STREAM_MAX_SECONDS", "45")

    settings = config_module.Settings.from_env()

    assert settings.stream_idle_seconds == 45


def test_config_stream_idle_seconds_zero_disables(monkeypatch) -> None:
    monkeypatch.setenv("CURSOR_STREAM_IDLE_SECONDS", "0")
    monkeypatch.setattr(config_module, "CONFIG_CURSOR_STREAM_IDLE_SECONDS", "")

    settings = config_module.Settings.from_env()

    assert settings.stream_idle_seconds == 0


def test_config_agent_schedule_default(monkeypatch) -> None:
    monkeypatch.delenv("AGENT_SCHEDULE", raising=False)
    monkeypatch.setattr(config_module, "CONFIG_AGENT_SCHEDULE", "")

    settings = config_module.Settings.from_env()

    assert settings.agent_schedule == ("claude", "cursor")


def test_config_agent_schedule_from_env(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_SCHEDULE", "cursor,claude")
    monkeypatch.setattr(config_module, "CONFIG_AGENT_SCHEDULE", "")

    settings = config_module.Settings.from_env()

    assert settings.agent_schedule == ("cursor", "claude")


def test_config_approve_mcps_from_env_when_empty(monkeypatch) -> None:
    monkeypatch.setenv("CURSOR_APPROVE_MCPS", "true")
    monkeypatch.setattr(config_module, "CONFIG_CURSOR_APPROVE_MCPS", "")

    settings = config_module.Settings.from_env()

    assert settings.approve_mcps is True
