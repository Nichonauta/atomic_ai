async def test_second_turn_does_not_redecompose_first_turn(client, fake_upstream):
    fake_upstream.queue_completion(content='{"atomic": true, "subtasks": []}')
    fake_upstream.queue_stream(pieces=["resultado turno 1"])
    fake_upstream.queue_stream(pieces=["respuesta turno 1"])

    payload1 = {
        "model": "test-model",
        "messages": [
            {"role": "system", "content": "eres un asistente"},
            {"role": "user", "content": "haz A"},
        ],
    }
    resp1 = await client.post("/v1/chat/completions", json=payload1)
    assert resp1.status_code == 200
    assert resp1.json()["choices"][0]["message"]["content"] == "respuesta turno 1"
    assert len(fake_upstream.received) == 3

    fake_upstream.queue_completion(content='{"atomic": true, "subtasks": []}')
    fake_upstream.queue_stream(pieces=["resultado turno 2"])
    fake_upstream.queue_stream(pieces=["respuesta turno 2"])

    payload2 = {
        "model": "test-model",
        "messages": payload1["messages"]
        + [
            {"role": "assistant", "content": "respuesta turno 1"},
            {"role": "user", "content": "haz B"},
        ],
    }
    resp2 = await client.post("/v1/chat/completions", json=payload2)
    assert resp2.status_code == 200
    assert resp2.json()["choices"][0]["message"]["content"] == "respuesta turno 2"

    # Solo 3 llamadas nuevas para el turno 2: el turno 1 no se re-decompuso.
    assert len(fake_upstream.received) == 6

    decomp2_user_msg = fake_upstream.received[3]["messages"][1]["content"]
    assert "haz B" in decomp2_user_msg


async def test_new_turn_without_valid_session_still_answers_correctly(client, fake_upstream):
    fake_upstream.queue_completion(content='{"atomic": true, "subtasks": []}')
    fake_upstream.queue_stream(pieces=["resultado"])
    fake_upstream.queue_stream(pieces=["respuesta final"])

    payload = {
        "messages": [
            {"role": "system", "content": "eres un asistente"},
            {"role": "user", "content": "turno 1 ya resuelto en otro proceso"},
            {"role": "assistant", "content": "ok ya lo hice"},
            {"role": "user", "content": "ahora haz C"},
        ],
    }
    resp = await client.post("/v1/chat/completions", json=payload)
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "respuesta final"

    decomp_user_msg = fake_upstream.received[0]["messages"][1]["content"]
    tarea_section = decomp_user_msg.split("<tarea_a_evaluar>")[1].split("</tarea_a_evaluar>")[0]
    historial_section = decomp_user_msg.split("<historial_conversacion>")[1].split(
        "</historial_conversacion>"
    )[0]
    assert "ahora haz C" in tarea_section
    assert "turno 1 ya resuelto" not in tarea_section
    assert "turno 1 ya resuelto" in historial_section


async def test_expose_reasoning_content_false_hides_reasoning(client, fake_upstream, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "expose_reasoning_content", False)

    fake_upstream.queue_completion(content='{"atomic": true, "subtasks": []}')
    fake_upstream.queue_stream(pieces=["resultado"])
    fake_upstream.queue_stream(pieces=["respuesta"])

    payload = {"messages": [{"role": "user", "content": "hola"}]}
    resp = await client.post("/v1/chat/completions", json=payload)
    body = resp.json()
    assert body["choices"][0]["message"]["reasoning_content"] is None
    assert body["choices"][0]["message"]["content"] == "respuesta"


async def test_tool_call_round_trip_through_http_does_not_redecompose(client, fake_upstream):
    fake_upstream.queue_completion(content='{"atomic": true, "subtasks": []}')
    fake_upstream.queue_stream(tool_calls=[{"id": "call_1", "function": {"name": "leer", "arguments": "{}"}}])

    payload = {
        "messages": [{"role": "user", "content": "lee el archivo x"}],
        "tools": [{"type": "function", "function": {"name": "leer"}}],
    }
    resp1 = await client.post("/v1/chat/completions", json=payload)
    assert resp1.status_code == 200
    body1 = resp1.json()
    assert body1["choices"][0]["finish_reason"] == "tool_calls"
    tool_calls = body1["choices"][0]["message"]["tool_calls"]
    assert tool_calls[0]["id"] == "call_1"

    fake_upstream.queue_stream(pieces=["listo con el archivo"])
    fake_upstream.queue_stream(pieces=["respuesta final"])

    payload2 = {
        "messages": payload["messages"]
        + [
            {"role": "assistant", "content": None, "tool_calls": tool_calls},
            {"role": "tool", "tool_call_id": "call_1", "content": "contenido del archivo x"},
        ],
        "tools": payload["tools"],
    }
    resp2 = await client.post("/v1/chat/completions", json=payload2)
    assert resp2.status_code == 200
    assert resp2.json()["choices"][0]["message"]["content"] == "respuesta final"

    decomposition_calls = [r for r in fake_upstream.received if r.get("response_format")]
    assert len(decomposition_calls) == 1
