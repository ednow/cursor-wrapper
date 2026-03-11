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
