from app.main import _extract_goal_context
from app.schemas import ChatMessage


def _m(role, content=None, tool_calls=None, name=None, tool_call_id=None):
    return ChatMessage(
        role=role, content=content, tool_calls=tool_calls, name=name, tool_call_id=tool_call_id
    )


def test_single_turn_no_prior_assistant():
    messages = [
        _m("system", "eres un asistente"),
        _m("user", "hola, cuanto es 2+2"),
    ]
    ctx = _extract_goal_context(messages)
    assert ctx.caller_system == "eres un asistente"
    assert ctx.prior_context == ""
    assert "hola, cuanto es 2+2" in ctx.turn_instruction
    assert "[system]" not in ctx.turn_instruction
    assert "[system]" not in ctx.prior_context


def test_second_turn_keeps_prior_context_separate_from_new_turn():
    messages = [
        _m("system", "eres un asistente"),
        _m("user", "turno 1: haz A"),
        _m("assistant", "hecho A"),
        _m("user", "turno 2: haz B"),
    ]
    ctx = _extract_goal_context(messages)
    assert "turno 1: haz A" in ctx.prior_context
    assert "hecho A" in ctx.prior_context
    assert "turno 2: haz B" in ctx.turn_instruction
    # el turno 2 no debe filtrarse dentro del contexto previo, ni viceversa
    assert "turno 2: haz B" not in ctx.prior_context
    assert "turno 1: haz A" not in ctx.turn_instruction


def test_multiple_system_messages_are_concatenated_in_order():
    messages = [
        _m("system", "instrucción base"),
        _m("system", "instrucción adicional"),
        _m("user", "hola"),
    ]
    ctx = _extract_goal_context(messages)
    assert ctx.caller_system == "instrucción base\n\ninstrucción adicional"


def test_image_from_earlier_turn_still_present_for_current_turn():
    image_part = {"type": "image_url", "image_url": {"url": "data:image/png;base64,xxx"}}
    messages = [
        _m("system", "s"),
        _m("user", [{"type": "text", "text": "aquí una captura"}, image_part]),
        _m("assistant", "vi la imagen"),
        _m("user", "¿y ahora qué opinas del botón rojo?"),
    ]
    ctx = _extract_goal_context(messages)
    assert image_part in ctx.image_parts
    assert "y ahora qué opinas del botón rojo" in ctx.turn_instruction


def test_prior_context_override_is_used_instead_of_reflattening():
    messages = [
        _m("system", "s"),
        _m("user", "turno 1"),
        _m("assistant", "respuesta 1"),
        _m("user", "turno 2"),
    ]
    ctx = _extract_goal_context(messages, prior_context_override="resumen de síntesis previa")
    assert ctx.prior_context == "resumen de síntesis previa"
    assert "turno 1" not in ctx.prior_context


def test_assistant_turn_with_only_tool_calls_does_not_split_the_exchange():
    # Un assistant que solo llamó una tool (sin texto final) no cierra turno:
    # su tool_calls y el resultado de esa tool deben quedar juntos dentro de
    # turn_instruction, no partidos entre prior_context y turn_instruction.
    messages = [
        _m("user", "haz algo"),
        _m("assistant", None, tool_calls=[{"id": "c1", "function": {"name": "f", "arguments": "{}"}}]),
        _m("tool", "resultado", tool_call_id="c1", name="f"),
        _m("user", "ahora haz otra cosa"),
    ]
    ctx = _extract_goal_context(messages)
    assert ctx.prior_context == ""
    assert "haz algo" in ctx.turn_instruction
    assert "resultado de herramienta f" in ctx.turn_instruction
    assert "ahora haz otra cosa" in ctx.turn_instruction


def test_assistant_final_text_after_tool_exchange_closes_the_turn():
    # En cambio, si el assistant SÍ terminó con texto real tras el tool
    # exchange, ese texto marca el cierre de turno y todo lo anterior
    # (incluido el tool exchange ya resuelto) pasa a ser prior_context.
    messages = [
        _m("user", "haz algo"),
        _m("assistant", None, tool_calls=[{"id": "c1", "function": {"name": "f", "arguments": "{}"}}]),
        _m("tool", "resultado", tool_call_id="c1", name="f"),
        _m("assistant", "listo, ya lo hice"),
        _m("user", "ahora haz otra cosa"),
    ]
    ctx = _extract_goal_context(messages)
    assert "haz algo" in ctx.prior_context
    assert "resultado de herramienta f" in ctx.prior_context
    assert "listo, ya lo hice" in ctx.prior_context
    assert ctx.turn_instruction.strip() == "[user] ahora haz otra cosa"
