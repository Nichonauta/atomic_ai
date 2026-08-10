from app.upstream import UpstreamClient


async def test_fake_upstream_completion_roundtrip(fake_upstream):
    fake_upstream.queue_completion(content='{"atomic": true, "subtasks": []}')

    client = UpstreamClient()
    result = await client.complete(
        [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
        model="test-model",
        json_mode=True,
    )

    assert result == '{"atomic": true, "subtasks": []}'
    assert len(fake_upstream.received) == 1
    assert fake_upstream.received[0]["stream"] is False
    assert fake_upstream.received[0]["response_format"] == {"type": "json_object"}


async def test_fake_upstream_stream_content_and_tool_calls(fake_upstream):
    fake_upstream.queue_stream(pieces=["Hola ", "mundo"])
    fake_upstream.queue_stream(
        tool_calls=[{"id": "call_1", "function": {"name": "leer_archivo", "arguments": '{"path":"a.py"}'}}]
    )

    client = UpstreamClient()

    text_chunks = []
    async for piece in client.stream([{"role": "user", "content": "u"}], model="test-model"):
        text_chunks.append(piece)
    assert "".join(text_chunks) == "Hola mundo"

    tool_deltas = []
    finish_reasons = []
    async for chunk in client.stream_raw(
        [{"role": "user", "content": "u"}],
        model="test-model",
        tools=[{"type": "function", "function": {"name": "leer_archivo"}}],
        tool_choice="auto",
    ):
        if chunk["delta"].get("tool_calls"):
            tool_deltas.extend(chunk["delta"]["tool_calls"])
        finish_reasons.append(chunk["finish_reason"])

    assert tool_deltas[0]["function"]["name"] == "leer_archivo"
    assert finish_reasons[-1] == "tool_calls"
    assert len(fake_upstream.received) == 2
