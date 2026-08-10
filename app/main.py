from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse

from . import sse
from .config import settings
from .content import split_content
from .engine import AtomicDecompositionEngine, Event, GoalContext
from .schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    Choice,
    ResponseMessage,
)
from .session import (
    SessionState,
    SessionStore,
    extract_tool_outputs,
    hash_chain,
    is_new_turn,
    is_valid_resume,
    new_session_id,
)
from .upstream import UpstreamClient, UpstreamError

app = FastAPI(title="Atomic Decomposition Proxy")

session_store = SessionStore(ttl_seconds=settings.session_ttl_seconds, max_sessions=settings.max_sessions)


def _flatten_messages(messages: list[ChatMessage]) -> str:
    """Aplana una lista de mensajes a texto plano, tolerante a content
    multimodal (usa solo la parte de texto; las imágenes se extraen aparte en
    _extract_goal_context). Se usa para renderizar tanto el contexto previo
    como la instrucción del turno actual como texto de fondo."""
    parts = []
    for message in messages:
        text, _ = split_content(message.content)
        if message.role == "tool" and text:
            label = f"resultado de herramienta {message.name}" if message.name else "resultado de herramienta"
            parts.append(f"[{label}] {text}")
        elif text:
            parts.append(f"[{message.role}] {text}")
        elif message.tool_calls:
            calls_desc = "; ".join(
                f"{tc.get('function', {}).get('name')}({tc.get('function', {}).get('arguments')})"
                for tc in message.tool_calls
            )
            parts.append(f"[{message.role}] (llamó a herramienta(s): {calls_desc})")
    return "\n\n".join(parts)


def _find_turn_boundary(messages: list[ChatMessage]) -> int:
    """Índice del último mensaje assistant con texto real (una respuesta final
    ya entregada). Todo lo posterior es "el turno actual" (se decompone); todo
    lo anterior es contexto de fondo (nunca se redecompone).

    Un assistant que solo hizo tool_calls sin texto no cuenta como cierre de
    turno: es un intercambio todavía abierto, y debe quedar junto con su
    resultado de tool dentro de turn_instruction en vez de partirse entre
    prior_context y turn_instruction."""
    boundary = -1
    for i, message in enumerate(messages):
        if message.role == "assistant":
            text, _ = split_content(message.content)
            if text:
                boundary = i
    return boundary


def _extract_goal_context(
    messages: list[ChatMessage], prior_context_override: Optional[str] = None
) -> GoalContext:
    """Separa una request en: system prompt real del caller (autoridad, no
    dato), instrucción del turno actual, contexto de turnos previos, y partes
    de imagen de toda la conversación. Reemplaza el aplanado uniforme de
    _build_goal — es lo único que hace posible que un cliente agéntico
    multi-turno no vea su turno 1 redecompuesto/reinterpretado cada vez que
    manda un turno nuevo."""
    system_texts = [split_content(m.content)[0] for m in messages if m.role == "system"]
    caller_system = "\n\n".join(t for t in system_texts if t)

    boundary = _find_turn_boundary(messages)
    before = [m for m in messages[: boundary + 1] if m.role != "system"]
    after = [m for m in messages[boundary + 1 :] if m.role != "system"]

    turn_instruction = _flatten_messages(after)
    prior_context = (
        prior_context_override if prior_context_override is not None else _flatten_messages(before)
    )

    if not turn_instruction.strip():
        # Caso borde defensivo: si no hay nada claramente "posterior al último
        # turno del assistant" (p. ej. la request no sigue el patrón usual),
        # no dejamos que el motor decomponga una instrucción vacía.
        turn_instruction = _flatten_messages([m for m in messages if m.role != "system"])
        prior_context = prior_context_override if prior_context_override is not None else ""

    image_parts: list[dict[str, Any]] = []
    for message in messages:
        _, parts_found = split_content(message.content)
        image_parts.extend(parts_found)

    return GoalContext(
        caller_system=caller_system,
        turn_instruction=turn_instruction,
        prior_context=prior_context,
        image_parts=image_parts,
    )


def _serialize_messages(request: ChatCompletionRequest) -> list[dict[str, Any]]:
    return [m.model_dump(exclude_none=True) for m in request.messages]


@dataclass
class PreparedRun:
    engine: AtomicDecompositionEngine
    events: AsyncIterator[Event]
    session_id: str
    goal_ctx: GoalContext
    model: str
    tools: Optional[list[dict[str, Any]]]
    tool_choice: Any
    messages: list[dict[str, Any]]
    resumed_session: Optional[SessionState]
    turn_history: list[str] = field(default_factory=list)


async def _resolve_run(request: ChatCompletionRequest, client: UpstreamClient, requested_model: str) -> PreparedRun:
    """Resuelve una request en una de tres vías: (1) reanudación de una fase
    pausada por tool call, sin redecomponer ni reejecutar nada ya resuelto;
    (2) turno nuevo sobre una conversación con sesión viva ya completada, que
    arranca un run nuevo sembrado con el turn_history acumulado en vez de
    reaplanar/redecomponer el historial crudo; (3) sin sesión utilizable
    (primera vez, o TTL expirado) — fallback correcto por sí mismo gracias a
    _extract_goal_context, solo más caro."""
    messages = _serialize_messages(request)
    session = await session_store.find_matching(messages)

    if session and is_valid_resume(session, messages):
        tool_outputs = extract_tool_outputs(session, messages)
        engine = AtomicDecompositionEngine(
            client, session.model, tools=session.tools, tool_choice=session.tool_choice
        )
        engine.goal_ctx = session.goal_ctx
        engine.root = session.root
        engine.leaves = session.leaves
        engine.results = list(session.results)
        engine.pending_phase = session.pending_phase
        engine.pending_leaf_index = session.pending_leaf_index
        engine.pending_tool_calls = session.pending_tool_calls
        engine.pending_conversation = list(session.pending_conversation)
        engine.tool_round_count = session.tool_round_count
        events = engine.resume(tool_outputs)
        return PreparedRun(
            engine=engine,
            events=events,
            session_id=session.session_id,
            goal_ctx=session.goal_ctx,
            model=session.model,
            tools=session.tools,
            tool_choice=session.tool_choice,
            messages=messages,
            resumed_session=session,
            turn_history=session.turn_history,
        )

    if session and is_new_turn(session, messages):
        goal_ctx = _extract_goal_context(
            request.messages, prior_context_override="\n\n".join(session.turn_history)
        )
        engine = AtomicDecompositionEngine(
            client, requested_model, tools=request.tools, tool_choice=request.tool_choice
        )
        engine.goal_ctx = goal_ctx
        events = engine.run()
        return PreparedRun(
            engine=engine,
            events=events,
            session_id=new_session_id(),
            goal_ctx=goal_ctx,
            model=requested_model,
            tools=request.tools,
            tool_choice=request.tool_choice,
            messages=messages,
            resumed_session=None,
            turn_history=session.turn_history,
        )

    goal_ctx = _extract_goal_context(request.messages)
    engine = AtomicDecompositionEngine(
        client, requested_model, tools=request.tools, tool_choice=request.tool_choice
    )
    engine.goal_ctx = goal_ctx
    events = engine.run()
    return PreparedRun(
        engine=engine,
        events=events,
        session_id=new_session_id(),
        goal_ctx=goal_ctx,
        model=requested_model,
        tools=request.tools,
        tool_choice=request.tool_choice,
        messages=messages,
        resumed_session=None,
        turn_history=[],
    )


async def _persist_session(prepared: PreparedRun, *, paused: bool, final_content: str = "") -> None:
    engine = prepared.engine
    chain = hash_chain(prepared.messages)
    lock = prepared.resumed_session.lock if prepared.resumed_session else asyncio.Lock()

    turn_history = list(prepared.turn_history)
    if not paused and final_content:
        turn_history.append(final_content)

    state = SessionState(
        session_id=prepared.session_id,
        checkpoint_hash=chain[-1] if chain else "",
        checkpoint_len=len(prepared.messages),
        goal_ctx=prepared.goal_ctx,
        model=prepared.model,
        tools=prepared.tools,
        tool_choice=prepared.tool_choice,
        root=engine.root,
        leaves=engine.leaves,
        results=list(engine.results),
        pending_phase=engine.pending_phase if paused else None,
        pending_leaf_index=engine.pending_leaf_index if paused else None,
        pending_tool_calls=engine.pending_tool_calls if paused else [],
        pending_conversation=list(engine.pending_conversation) if paused else [],
        tool_round_count=engine.tool_round_count if paused else 0,
        turn_history=turn_history,
        lock=lock,
    )
    await session_store.save(state)


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@app.get("/v1/models")
async def list_models() -> dict:
    return {
        "object": "list",
        "data": [{"id": settings.upstream_model, "object": "model", "owned_by": "atomic-proxy"}],
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: ChatCompletionRequest):
    requested_model = request.model or settings.upstream_model
    client = UpstreamClient()
    prepared = await _resolve_run(request, client, requested_model)
    lock = prepared.resumed_session.lock if prepared.resumed_session else None

    if request.stream:
        return StreamingResponse(
            _stream_response(prepared, lock),
            media_type="text/event-stream",
        )

    if lock:
        await lock.acquire()
    try:
        reasoning_parts: list[str] = []
        content_parts: list[str] = []
        tool_calls: list[dict] | None = None
        try:
            async for kind, payload in prepared.events:
                if kind == "reasoning":
                    if settings.expose_reasoning_content:
                        reasoning_parts.append(payload)
                elif kind == "content":
                    content_parts.append(payload)
                elif kind == "tool_calls":
                    tool_calls = payload
        except UpstreamError as exc:
            return JSONResponse(
                status_code=502,
                content={"error": {"message": str(exc), "type": "upstream_error"}},
            )

        final_content = "".join(content_parts)
        await _persist_session(prepared, paused=bool(tool_calls), final_content=final_content)
    finally:
        if lock:
            lock.release()

    response = ChatCompletionResponse(
        model=prepared.model,
        choices=[
            Choice(
                message=ResponseMessage(
                    content=final_content if content_parts else None,
                    reasoning_content=("".join(reasoning_parts) if settings.expose_reasoning_content else None),
                    tool_calls=tool_calls,
                ),
                finish_reason="tool_calls" if tool_calls else "stop",
            )
        ],
    )
    return response


async def _stream_response(prepared: PreparedRun, lock: Optional[asyncio.Lock]):
    model = prepared.model
    chunk_id = f"chatcmpl-{uuid.uuid4().hex}"
    yield sse.role_chunk(model, chunk_id)

    if lock:
        await lock.acquire()
    try:
        content_parts: list[str] = []
        try:
            async for kind, payload in prepared.events:
                if kind == "reasoning":
                    if settings.expose_reasoning_content:
                        yield sse.reasoning_chunk(model, payload, chunk_id)
                elif kind == "content":
                    content_parts.append(payload)
                    yield sse.content_chunk(model, payload, chunk_id)
                elif kind == "tool_calls":
                    await _persist_session(prepared, paused=True, final_content="".join(content_parts))
                    yield sse.raw_delta_chunk(model, {"tool_calls": payload}, chunk_id)
                    yield sse.final_chunk(model, chunk_id, finish_reason="tool_calls")
                    yield sse.done()
                    return
        except UpstreamError as exc:
            error_payload = {"error": {"message": str(exc), "type": "upstream_error"}}
            yield f"data: {json.dumps(error_payload)}\n\n"
            yield sse.done()
            return

        await _persist_session(prepared, paused=False, final_content="".join(content_parts))
        yield sse.final_chunk(model, chunk_id)
        yield sse.done()
    finally:
        if lock:
            lock.release()
