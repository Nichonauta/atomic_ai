from app.engine import GoalContext, TaskNode
from app.session import SessionState, is_new_turn, is_valid_resume


def _base_session(**overrides) -> SessionState:
    defaults = dict(
        session_id="s1",
        checkpoint_hash="h1",
        checkpoint_len=2,
        goal_ctx=GoalContext(caller_system="", turn_instruction="haz algo", prior_context=""),
        model="test-model",
        tools=None,
        tool_choice=None,
        root=TaskNode(description="haz algo", depth=0, is_atomic=True),
        leaves=[],
        results=[],
    )
    defaults.update(overrides)
    return SessionState(**defaults)


def test_paused_in_leaf_is_valid_resume_when_tool_output_present():
    session = _base_session(
        pending_phase="leaf",
        pending_leaf_index=0,
        pending_tool_calls=[{"id": "call_1", "function": {"name": "f", "arguments": "{}"}}],
    )
    messages = [{"role": "user"}, {"role": "user"}, {"role": "tool", "tool_call_id": "call_1", "content": "r"}]
    assert is_valid_resume(session, messages) is True
    assert is_new_turn(session, messages) is False


def test_paused_in_synthesis_is_valid_resume_when_tool_output_present():
    session = _base_session(
        pending_phase="synthesis",
        pending_leaf_index=None,
        pending_tool_calls=[{"id": "call_s1", "function": {"name": "verificar", "arguments": "{}"}}],
    )
    messages = [{"role": "user"}, {"role": "user"}, {"role": "tool", "tool_call_id": "call_s1", "content": "ok"}]
    assert is_valid_resume(session, messages) is True
    assert is_new_turn(session, messages) is False


def test_completed_session_without_new_messages_is_neither():
    session = _base_session(pending_phase=None, pending_tool_calls=[])
    messages = [{"role": "user"}, {"role": "user"}]  # == checkpoint_len, nada nuevo
    assert is_valid_resume(session, messages) is False
    assert is_new_turn(session, messages) is False


def test_completed_session_with_new_trailing_messages_is_new_turn():
    session = _base_session(pending_phase=None, pending_tool_calls=[])
    messages = [{"role": "user"}, {"role": "user"}, {"role": "user", "content": "otra cosa"}]
    assert is_valid_resume(session, messages) is False
    assert is_new_turn(session, messages) is True


def test_pending_leaf_but_missing_tool_output_is_not_valid_resume():
    session = _base_session(
        pending_phase="leaf",
        pending_leaf_index=0,
        pending_tool_calls=[{"id": "call_1", "function": {"name": "f", "arguments": "{}"}}],
    )
    # hay mensajes nuevos pero ninguno resuelve la tool call pendiente
    messages = [{"role": "user"}, {"role": "user"}, {"role": "user", "content": "otra cosa"}]
    assert is_valid_resume(session, messages) is False
    assert is_new_turn(session, messages) is False  # sigue pendiente, no es "turno nuevo"
