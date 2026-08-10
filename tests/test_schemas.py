from app.schemas import ChatCompletionRequest


def test_chat_message_accepts_plain_string_content():
    req = ChatCompletionRequest.model_validate(
        {"messages": [{"role": "user", "content": "hola"}]}
    )
    assert req.messages[0].content == "hola"


def test_chat_message_accepts_multimodal_content_list():
    payload = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe esta imagen"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,xxx"},
                    },
                ],
            }
        ]
    }
    # Antes de soportar content multimodal, esto fallaba con un ValidationError
    # (equivalente a un 422 en la capa HTTP) porque content solo aceptaba str.
    req = ChatCompletionRequest.model_validate(payload)
    assert isinstance(req.messages[0].content, list)
    assert req.messages[0].content[1]["type"] == "image_url"


def test_chat_message_accepts_multimodal_content_in_tool_role():
    payload = {
        "messages": [
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": [
                    {"type": "text", "text": "captura de pantalla adjunta"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,yyy"}},
                ],
            }
        ]
    }
    req = ChatCompletionRequest.model_validate(payload)
    assert isinstance(req.messages[0].content, list)
