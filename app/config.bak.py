from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

CONFIG_CURSOR_BIN = r""
CONFIG_CURSOR_WORKSPACE = ""
# 暴露给外部调用是用来验证的 API Key
CONFIG_WRAPPER_API_KEY = ""
# Cursor API Key
CONFIG_CURSOR_API_KEY = ""
CONFIG_DEFAULT_MODEL = ""
CONFIG_MODEL_ALIASES = ""
CONFIG_CURSOR_TRUST = ""
CONFIG_CURSOR_APPROVE_MCPS = ""
CONFIG_CURSOR_FORCE = ""
CONFIG_CURSOR_SANDBOX = ""
# 是否对工具调用 hint 启用精简模式（过滤嵌套参数 / 截断长值），默认开启
# True or False
CONFIG_TOOL_HINT_COMPACT = ""
# 精简模式下单个参数值的最大字符数，超出后截断并加 ...
# 80 或者 160
CONFIG_TOOL_HINT_MAX_VALUE_LEN = ""
# 日志级别：debug / info / warning / error / critical，默认 info
CONFIG_WRAPPER_LOG_LEVEL = "debug"
# 流式结束时 debug 日志里「preview」的最大字符数；留空或 <=0 表示不截断（全文）
CONFIG_LOG_RESPONSE_PREVIEW_MAX_LEN = ""
# 流式 NDJSON 读完后是否在生成器内同步等待子进程收尾（True=旧行为，含非零退出时抛错）；默认 False 为异步收尾
# True or False
CONFIG_CURSOR_STREAM_SYNC_CLI_REAP = ""

DEFAULT_MODEL_ALIASES = {
    "cursor-agent": "auto",
    # Claude / Opus
    "claude-opus-4.6": "opus-4.6",
    "claude-opus-4.6-thinking": "opus-4.6-thinking",
    "claude-opus-4.5": "opus-4.5",
    "claude-opus-4.5-thinking": "opus-4.5-thinking",
    # Claude / Sonnet
    "claude-sonnet-4.6": "sonnet-4.6",
    "claude-sonnet-4.6-thinking": "sonnet-4.6-thinking",
    "claude-sonnet-4.5": "sonnet-4.5",
    "claude-sonnet-4.5-thinking": "sonnet-4.5-thinking",
    # GPT
    "gpt-5.4": "gpt-5.4-medium",
    "gpt-5.4-fast": "gpt-5.4-medium-fast",
    "gpt-5.3": "gpt-5.3-codex",
    "gpt-5.2": "gpt-5.2",
    "gpt-5.1": "gpt-5.1-high",
    # Gemini
    "gemini-3.1-pro": "gemini-3.1-pro",
    "gemini-3-pro": "gemini-3-pro",
    "gemini-3-flash": "gemini-3-flash",
    # Grok
    "grok": "grok",
    # Kimi
    "kimi-k2.5": "kimi-k2.5",
}


def _pick_config_or_env(config_value: str, env_name: str, default: str | None = None) -> str | None:
    if config_value.strip():
        return config_value.strip()

    env_value = os.getenv(env_name)
    if env_value is not None:
        return env_value

    return default


def _parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_model_aliases(raw_value: str | None) -> dict[str, str]:
    aliases: dict[str, str] = {}
    if not raw_value:
        return aliases

    for pair in raw_value.split(","):
        item = pair.strip()
        if not item or "=" not in item:
            continue
        alias, target = item.split("=", 1)
        alias = alias.strip()
        target = target.strip()
        if alias and target:
            aliases[alias] = target

    return aliases


def _parse_log_level(raw: str | None) -> str:
    name = (raw or "info").strip().lower()
    allowed = {"critical", "error", "warning", "info", "debug", "notset"}
    return name if name in allowed else "info"


def _parse_optional_preview_len(raw: str | None) -> int | None:
    """未配置或无效或 <=0 时返回 None，表示日志预览不截断。"""
    if raw is None or not str(raw).strip():
        return None
    try:
        n = int(str(raw).strip(), 10)
    except ValueError:
        return None
    return None if n <= 0 else n


@dataclass(frozen=True)
class Settings:
    cursor_bin: str
    cursor_workspace: str
    wrapper_api_key: str | None
    default_model: str
    model_aliases: dict[str, str]
    trust_workspace: bool
    approve_mcps: bool
    force: bool
    sandbox: str | None
    cursor_api_key: str | None = None
    tool_hint_compact: bool = True
    tool_hint_max_value_len: int = 80
    log_level: str = "info"
    log_response_preview_max_len: int | None = None
    stream_sync_cli_reap: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        workspace = Path(
            _pick_config_or_env(CONFIG_CURSOR_WORKSPACE, "CURSOR_WORKSPACE", os.getcwd()) or os.getcwd()
        ).resolve()
        env_aliases = _parse_model_aliases(_pick_config_or_env(CONFIG_MODEL_ALIASES, "MODEL_ALIASES", ""))
        model_aliases = {**DEFAULT_MODEL_ALIASES, **env_aliases}
        return cls(
            cursor_bin=_pick_config_or_env(CONFIG_CURSOR_BIN, "CURSOR_BIN", "agent") or "agent",
            cursor_workspace=str(workspace),
            wrapper_api_key=_pick_config_or_env(CONFIG_WRAPPER_API_KEY, "WRAPPER_API_KEY"),
            cursor_api_key=_pick_config_or_env(CONFIG_CURSOR_API_KEY, "CURSOR_API_KEY"),
            default_model=_pick_config_or_env(CONFIG_DEFAULT_MODEL, "DEFAULT_MODEL", "cursor-agent")
            or "cursor-agent",
            model_aliases=model_aliases,
            trust_workspace=_parse_bool(
                _pick_config_or_env(CONFIG_CURSOR_TRUST, "CURSOR_TRUST"),
                default=True,
            ),
            approve_mcps=_parse_bool(
                _pick_config_or_env(CONFIG_CURSOR_APPROVE_MCPS, "CURSOR_APPROVE_MCPS"),
            ),
            force=_parse_bool(
                _pick_config_or_env(CONFIG_CURSOR_FORCE, "CURSOR_FORCE"),
            ),
            sandbox=_pick_config_or_env(CONFIG_CURSOR_SANDBOX, "CURSOR_SANDBOX"),
            tool_hint_compact=_parse_bool(
                _pick_config_or_env(CONFIG_TOOL_HINT_COMPACT, "TOOL_HINT_COMPACT"),
                default=True,
            ),
            tool_hint_max_value_len=int(
                _pick_config_or_env(CONFIG_TOOL_HINT_MAX_VALUE_LEN, "TOOL_HINT_MAX_VALUE_LEN", "80") or "80"
            ),
            log_level=_parse_log_level(
                _pick_config_or_env(CONFIG_WRAPPER_LOG_LEVEL, "WRAPPER_LOG_LEVEL", "info")
            ),
            log_response_preview_max_len=_parse_optional_preview_len(
                _pick_config_or_env(
                    CONFIG_LOG_RESPONSE_PREVIEW_MAX_LEN,
                    "WRAPPER_LOG_RESPONSE_PREVIEW_MAX_LEN",
                )
            ),
            stream_sync_cli_reap=_parse_bool(
                _pick_config_or_env(
                    CONFIG_CURSOR_STREAM_SYNC_CLI_REAP,
                    "CURSOR_STREAM_SYNC_CLI_REAP",
                ),
                default=False,
            ),
        )

    def resolve_model(self, requested_model: str | None) -> str:
        candidate = requested_model or self.default_model
        return self.model_aliases.get(candidate, candidate)

    def exposed_model(self, requested_model: str | None) -> str:
        return requested_model or self.default_model

    def list_models(self) -> list[str]:
        exposed = {self.default_model, *self.model_aliases.keys()}
        return sorted(exposed)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
