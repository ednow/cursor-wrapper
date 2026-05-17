import json

import pytest

from app.cli_base import (
    claude_cli_model_arg,
    claude_result_error_message,
    extract_claude_assistant_text,
)
from app.claude_cli import ClaudeCLIAdapter
from helpers import make_test_settings


def test_claude_cli_model_arg_skips_auto() -> None:
    assert claude_cli_model_arg("auto") is None
    assert claude_cli_model_arg("cursor-agent") is None
    assert claude_cli_model_arg("sonnet-4.6") == "sonnet-4.6"


def test_extract_claude_assistant_api_error() -> None:
    payload = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "text",
                    "text": 'API Error: 400 {"error":"BadRequest: 模型不存在或不可用"}',
                }
            ]
        },
    }
    assert "模型不存在" in extract_claude_assistant_text(payload)


def test_claude_result_error_message() -> None:
    payload = {
        "type": "result",
        "is_error": True,
        "result": "API Error: 400 model missing",
    }
    assert claude_result_error_message(payload) == "API Error: 400 model missing"


def test_build_command_omits_model_auto() -> None:
    adapter = ClaudeCLIAdapter(make_test_settings())
    cmd = adapter._build_command("hi", "auto", stream=True)
    assert "--model" not in cmd
    assert "auto" not in cmd


def test_build_command_includes_explicit_model() -> None:
    adapter = ClaudeCLIAdapter(make_test_settings())
    cmd = adapter._build_command("hi", "opus-4.6", stream=True)
    assert "--model" in cmd
    idx = cmd.index("--model")
    assert cmd[idx + 1] == "opus-4.6"
