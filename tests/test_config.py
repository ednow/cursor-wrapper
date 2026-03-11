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
