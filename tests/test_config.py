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
