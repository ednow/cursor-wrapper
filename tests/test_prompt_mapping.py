from app.openai_schema import ChatMessage, MessageContentPart, flatten_messages


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
