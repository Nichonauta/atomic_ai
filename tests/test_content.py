from app.content import build_multimodal_content, split_content


def test_split_content_none():
    assert split_content(None) == ("", [])


def test_split_content_plain_string():
    assert split_content("hola") == ("hola", [])


def test_split_content_list_only_text():
    content = [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
    text, parts = split_content(content)
    assert text == "ab"
    assert parts == []


def test_split_content_text_and_image():
    image_part = {"type": "image_url", "image_url": {"url": "data:image/png;base64,xxx"}}
    content = [{"type": "text", "text": "describe esto"}, image_part]
    text, parts = split_content(content)
    assert text == "describe esto"
    assert parts == [image_part]


def test_split_content_multiple_text_parts_preserve_order():
    content = [
        {"type": "text", "text": "primero "},
        {"type": "image_url", "image_url": {"url": "u1"}},
        {"type": "text", "text": "segundo"},
    ]
    text, parts = split_content(content)
    assert text == "primero segundo"
    assert len(parts) == 1


def test_build_multimodal_content_no_extra_parts_returns_plain_string():
    result = build_multimodal_content("hola", [])
    assert result == "hola"
    assert isinstance(result, str)


def test_build_multimodal_content_with_images_returns_list():
    image_part = {"type": "image_url", "image_url": {"url": "u"}}
    result = build_multimodal_content("hola", [image_part])
    assert result == [{"type": "text", "text": "hola"}, image_part]


def test_build_multimodal_content_empty_text_with_images_omits_empty_text_part():
    image_part = {"type": "image_url", "image_url": {"url": "u"}}
    result = build_multimodal_content("", [image_part])
    assert result == [image_part]


def test_roundtrip_split_then_build_is_stable():
    original = [
        {"type": "text", "text": "hola mundo"},
        {"type": "image_url", "image_url": {"url": "u"}},
    ]
    text, parts = split_content(original)
    rebuilt = build_multimodal_content(text, parts)
    assert rebuilt == original
