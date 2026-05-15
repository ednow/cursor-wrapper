from app.openai_schema import (
    ChatMessage,
    MessageContentPart,
    build_cli_prompt,
    flatten_messages,
)


def test_flatten_messages_keeps_role_boundaries() -> None:
    messages = [
        ChatMessage(role="system", content="You are concise."),
        ChatMessage(
            role="user",
            content=[
                MessageContentPart(type="text", text="Hello"),
                MessageContentPart(type="text", text=" world"),
            ],
        ),
        ChatMessage(role="assistant", content="Hi there"),
    ]

    prompt = flatten_messages(messages)

    assert "SYSTEM:\nYou are concise." in prompt
    assert "USER:\nHello world" in prompt
    assert "ASSISTANT:\nHi there" in prompt


def test_flatten_messages_includes_tool_messages() -> None:
    messages = [
        ChatMessage(role="user", content="What's the weather?"),
        ChatMessage(role="assistant", content="Calling weather tool..."),
        ChatMessage(
            role="tool",
            name="get_weather",
            tool_call_id="call_123",
            content="Sunny, 26C",
        ),
    ]

    prompt = flatten_messages(messages)

    assert "USER:\nWhat's the weather?" in prompt
    assert "ASSISTANT:\nCalling weather tool..." in prompt
    assert "TOOL[get_weather]:\nSunny, 26C" in prompt


def test_build_cli_prompt_bridge_single_user_no_file() -> None:
    messages = [ChatMessage(role="user", content="Only question")]
    p = build_cli_prompt(messages, last_user_context_bridge=True)
    assert p.startswith("我的问题：Only question")
    assert "未附带独立 prompt 文件" in p


def test_build_cli_prompt_bridge_with_prior_writes_sidecar() -> None:
    messages = [
        ChatMessage(role="user", content="First"),
        ChatMessage(role="assistant", content="Ok"),
        ChatMessage(role="user", content="Second"),
    ]

    def persist(s: str) -> str:
        assert "USER:\nFirst" in s
        assert "USER:\nSecond" in s
        return r"E:\tmp\fake-prompt.txt"

    p = build_cli_prompt(
        messages,
        last_user_context_bridge=True,
        persist_full_context=persist,
    )
    assert p.startswith("我的问题：Second")
    assert r"Prompt file: E:\tmp\fake-prompt.txt" in p


def test_build_cli_prompt_bridge_off_matches_flatten() -> None:
    messages = [
        ChatMessage(role="user", content="A"),
        ChatMessage(role="assistant", content="B"),
    ]
    assert build_cli_prompt(messages, last_user_context_bridge=False) == flatten_messages(messages)
