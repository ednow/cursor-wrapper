from app.openai_schema import ChatMessage, responses_input_to_chat_messages


def test_responses_input_string() -> None:
    msgs = responses_input_to_chat_messages("  hello  ")
    assert msgs == [ChatMessage(role="user", content="hello")]


def test_responses_input_message_list() -> None:
    msgs = responses_input_to_chat_messages(
        [
            {"type": "message", "role": "system", "content": "Be brief"},
            {"role": "user", "content": [{"type": "input_text", "text": "Hi"}]},
        ]
    )
    assert len(msgs) == 2
    assert msgs[0].role == "system"
    assert msgs[0].content == "Be brief"
    assert msgs[1].role == "user"
    assert msgs[1].content == "Hi"


def test_responses_input_function_call_output() -> None:
    msgs = responses_input_to_chat_messages(
        [
            {"role": "user", "content": "q"},
            {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": '{"ok": true}',
            },
        ]
    )
    assert msgs[1].role == "tool"
    assert msgs[1].tool_call_id == "call_1"
    assert msgs[1].content == '{"ok": true}'
