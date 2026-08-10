from app.engine import AtomicDecompositionEngine, GoalContext
from app.upstream import UpstreamClient


def _engine(**kwargs) -> AtomicDecompositionEngine:
    client = UpstreamClient()
    return AtomicDecompositionEngine(client, model="test-model", **kwargs)


async def test_full_run_basic_flow(fake_upstream):
    fake_upstream.queue_completion(content='{"atomic": true, "subtasks": []}')
    fake_upstream.queue_stream(pieces=["resultado de la tarea"])
    fake_upstream.queue_stream(pieces=["respuesta final"])

    engine = _engine()
    engine.goal_ctx = GoalContext(caller_system="", turn_instruction="haz algo simple", prior_context="")

    events = [e async for e in engine.run()]

    content_text = "".join(p for k, p in events if k == "content")
    assert content_text == "respuesta final"
    assert not any(k == "tool_calls" for k, _ in events)


async def test_multi_round_tool_calls_preserve_history_within_leaf(fake_upstream):
    fake_upstream.queue_completion(content='{"atomic": true, "subtasks": []}')
    fake_upstream.queue_stream(tool_calls=[{"id": "call_1", "function": {"name": "leer", "arguments": "{}"}}])
    fake_upstream.queue_stream(tool_calls=[{"id": "call_2", "function": {"name": "escribir", "arguments": "{}"}}])
    fake_upstream.queue_stream(pieces=["listo"])
    fake_upstream.queue_stream(pieces=["respuesta final"])

    engine = _engine(tools=[{"type": "function", "function": {"name": "leer"}}])
    engine.goal_ctx = GoalContext(caller_system="", turn_instruction="usa herramientas", prior_context="")

    async for kind, _ in engine.run():
        if kind == "tool_calls":
            break
    assert engine.pending_phase == "leaf"
    assert engine.pending_tool_calls[0]["id"] == "call_1"

    async for kind, _ in engine.resume({"call_1": "contenido leído"}):
        if kind == "tool_calls":
            break
    assert engine.pending_phase == "leaf"
    assert engine.pending_tool_calls[0]["id"] == "call_2"

    round2_request = fake_upstream.received[2]
    round2_tool_ids = [m.get("tool_call_id") for m in round2_request["messages"] if m.get("role") == "tool"]
    assert "call_1" in round2_tool_ids

    events3 = [e async for e in engine.resume({"call_2": "escrito ok"})]

    round3_request = fake_upstream.received[3]
    round3_tool_ids = [m.get("tool_call_id") for m in round3_request["messages"] if m.get("role") == "tool"]
    assert "call_1" in round3_tool_ids and "call_2" in round3_tool_ids

    content_text = "".join(p for k, p in events3 if k == "content")
    assert content_text == "respuesta final"


async def test_tool_call_during_synthesis_is_resumable(fake_upstream):
    fake_upstream.queue_completion(content='{"atomic": true, "subtasks": []}')
    fake_upstream.queue_stream(pieces=["resultado atómico"])
    fake_upstream.queue_stream(tool_calls=[{"id": "call_s1", "function": {"name": "verificar", "arguments": "{}"}}])
    fake_upstream.queue_stream(pieces=["respuesta final verificada"])

    engine = _engine(tools=[{"type": "function", "function": {"name": "verificar"}}])
    engine.goal_ctx = GoalContext(caller_system="", turn_instruction="haz algo y verifica", prior_context="")

    async for kind, _ in engine.run():
        if kind == "tool_calls":
            break

    assert engine.pending_phase == "synthesis"
    assert engine.pending_tool_calls[0]["id"] == "call_s1"

    events2 = [e async for e in engine.resume({"call_s1": "verificado ok"})]

    content_text = "".join(p for k, p in events2 if k == "content")
    assert content_text == "respuesta final verificada"
    assert engine.pending_phase is None


async def test_forced_tool_choice_from_caller_not_propagated_internally(fake_upstream):
    fake_upstream.queue_completion(content='{"atomic": true, "subtasks": []}')
    fake_upstream.queue_stream(pieces=["ok"])
    fake_upstream.queue_stream(pieces=["final"])

    engine = _engine(
        tools=[{"type": "function", "function": {"name": "f"}}],
        tool_choice={"type": "function", "function": {"name": "f"}},
    )
    engine.goal_ctx = GoalContext(caller_system="", turn_instruction="haz algo", prior_context="")

    async for _ in engine.run():
        pass

    assert fake_upstream.received[1]["tool_choice"] == "auto"
    assert fake_upstream.received[2]["tool_choice"] == "auto"


async def test_tool_round_limit_forces_final_text(fake_upstream, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "max_tool_rounds_per_phase", 2)

    fake_upstream.queue_completion(content='{"atomic": true, "subtasks": []}')
    fake_upstream.queue_stream(tool_calls=[{"id": "c1", "function": {"name": "f", "arguments": "{}"}}])
    fake_upstream.queue_stream(tool_calls=[{"id": "c2", "function": {"name": "f", "arguments": "{}"}}])
    fake_upstream.queue_stream(pieces=["forzado a texto"])
    fake_upstream.queue_stream(pieces=["síntesis"])

    engine = _engine(tools=[{"type": "function", "function": {"name": "f"}}])
    engine.goal_ctx = GoalContext(
        caller_system="", turn_instruction="intenta usar tools sin parar", prior_context=""
    )

    async for kind, _ in engine.run():
        if kind == "tool_calls":
            break
    assert engine.tool_round_count == 1

    async for kind, _ in engine.resume({"c1": "r1"}):
        if kind == "tool_calls":
            break
    assert engine.tool_round_count == 2

    events3 = [e async for e in engine.resume({"c2": "r2"})]

    third_leaf_call = fake_upstream.received[3]
    assert not third_leaf_call.get("tools")
    assert not any(k == "tool_calls" for k, _ in events3)


async def test_caller_system_prompt_is_composed_not_replaced(fake_upstream):
    fake_upstream.queue_completion(content='{"atomic": true, "subtasks": []}')
    fake_upstream.queue_stream(pieces=["ok"])
    fake_upstream.queue_stream(pieces=["final"])

    engine = _engine()
    engine.goal_ctx = GoalContext(
        caller_system="Eres Codex, un agente de código con reglas X.",
        turn_instruction="arregla el bug",
        prior_context="",
    )

    async for _ in engine.run():
        pass

    decomposition_system_msg = fake_upstream.received[0]["messages"][0]["content"]
    assert "Eres Codex, un agente de código con reglas X." in decomposition_system_msg
    assert "planificador de tareas" in decomposition_system_msg

    leaf_system_msg = fake_upstream.received[1]["messages"][0]["content"]
    assert "Eres Codex, un agente de código con reglas X." in leaf_system_msg


async def test_decomposition_sees_tool_descriptions_as_text(fake_upstream):
    fake_upstream.queue_completion(content='{"atomic": true, "subtasks": []}')
    fake_upstream.queue_stream(pieces=["ok"])
    fake_upstream.queue_stream(pieces=["final"])

    engine = _engine(
        tools=[
            {
                "type": "function",
                "function": {"name": "get_weather", "description": "Devuelve el clima de una ciudad"},
            }
        ]
    )
    engine.goal_ctx = GoalContext(caller_system="", turn_instruction="clima en Paris", prior_context="")

    async for _ in engine.run():
        pass

    decomposition_user_msg = fake_upstream.received[0]["messages"][1]["content"]
    assert "get_weather" in decomposition_user_msg
    assert "Devuelve el clima de una ciudad" in decomposition_user_msg
    # la Fase 1 sigue sin recibir tools funcionales (json_mode + tools es frágil)
    assert "tools" not in fake_upstream.received[0]


async def test_decomposition_without_tools_shows_placeholder(fake_upstream):
    fake_upstream.queue_completion(content='{"atomic": true, "subtasks": []}')
    fake_upstream.queue_stream(pieces=["ok"])
    fake_upstream.queue_stream(pieces=["final"])

    engine = _engine()
    engine.goal_ctx = GoalContext(caller_system="", turn_instruction="haz algo", prior_context="")

    async for _ in engine.run():
        pass

    decomposition_user_msg = fake_upstream.received[0]["messages"][1]["content"]
    assert "ninguna herramienta disponible" in decomposition_user_msg


async def test_images_are_attached_to_every_phase(fake_upstream):
    image_part = {"type": "image_url", "image_url": {"url": "data:image/png;base64,xxx"}}
    fake_upstream.queue_completion(content='{"atomic": true, "subtasks": []}')
    fake_upstream.queue_stream(pieces=["ok"])
    fake_upstream.queue_stream(pieces=["final"])

    engine = _engine()
    engine.goal_ctx = GoalContext(
        caller_system="",
        turn_instruction="describe la imagen",
        prior_context="",
        image_parts=[image_part],
    )

    async for _ in engine.run():
        pass

    for i in range(3):
        user_content = fake_upstream.received[i]["messages"][1]["content"]
        assert isinstance(user_content, list)
        assert image_part in user_content
