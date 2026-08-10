from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from .engine import GoalContext, TaskNode


def _message_digest(prev: str, message: dict[str, Any]) -> str:
    serialized = json.dumps(message, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(f"{prev}\x1e{serialized}".encode("utf-8")).hexdigest()


def hash_chain(messages: list[dict[str, Any]]) -> list[str]:
    """Hash acumulado por prefijo: chain[i] identifica de forma estable el
    historial messages[:i+1], para poder detectar cuándo una request nueva es
    continuación exacta (mismo prefijo) de una conversación ya vista."""
    chain: list[str] = []
    prev = ""
    for message in messages:
        prev = _message_digest(prev, message)
        chain.append(prev)
    return chain


def new_session_id() -> str:
    return uuid.uuid4().hex


@dataclass
class SessionState:
    session_id: str
    checkpoint_hash: str
    checkpoint_len: int
    goal_ctx: GoalContext
    model: str
    tools: Optional[list[dict[str, Any]]]
    tool_choice: Any
    root: TaskNode
    leaves: list[TaskNode]
    results: list[str]
    pending_phase: Optional[Literal["leaf", "synthesis"]] = None
    pending_leaf_index: Optional[int] = None
    pending_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    pending_conversation: list[dict[str, Any]] = field(default_factory=list)
    tool_round_count: int = 0
    # Solo el contenido final de cada síntesis ya entregada en turnos previos
    # de esta misma conversación — nunca el reasoning_content interno, para
    # no filtrar la narración del proxy como si fuera diálogo real.
    turn_history: list[str] = field(default_factory=list)
    last_used_at: float = field(default_factory=time.time)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def pending_tool_call_ids(self) -> set[str]:
        return {tc["id"] for tc in self.pending_tool_calls if tc.get("id")}


def is_valid_resume(session: SessionState, messages: list[dict[str, Any]]) -> bool:
    """True si `messages` extiende exactamente el checkpoint de `session` con
    los resultados de tool que esa sesión estaba esperando (en una hoja
    atómica o en la síntesis final — ambas quedan marcadas con pending_phase)."""
    if session.pending_phase is None or not session.pending_tool_calls:
        return False
    if len(messages) <= session.checkpoint_len:
        return False
    suffix = messages[session.checkpoint_len :]
    tool_ids = {m.get("tool_call_id") for m in suffix if m.get("role") == "tool" and m.get("tool_call_id")}
    return session.pending_tool_call_ids.issubset(tool_ids)


def is_new_turn(session: SessionState, messages: list[dict[str, Any]]) -> bool:
    """True si la sesión ya terminó su run (sin fase pendiente) pero la
    request trae mensajes nuevos más allá del checkpoint: un turno externo
    nuevo del caller sobre una conversación ya resuelta, no una reanudación
    de tool call. Permite sembrar el turno nuevo con turn_history en vez de
    reaplanar/redecomponer el historial crudo desde cero."""
    return session.pending_phase is None and len(messages) > session.checkpoint_len


def extract_tool_outputs(session: SessionState, messages: list[dict[str, Any]]) -> dict[str, str]:
    suffix = messages[session.checkpoint_len :]
    outputs: dict[str, str] = {}
    for m in suffix:
        if m.get("role") == "tool" and m.get("tool_call_id") in session.pending_tool_call_ids:
            outputs[m["tool_call_id"]] = m.get("content") or ""
    return outputs


class SessionStore:
    """Guarda el árbol de tareas y los resultados ya calculados entre
    peticiones HTTP, para poder reanudar un turno externo pausado por una
    tool call sin repetir la descomposición ni las tareas atómicas ya
    resueltas."""

    def __init__(self, ttl_seconds: float, max_sessions: int) -> None:
        self._ttl = ttl_seconds
        self._max_sessions = max_sessions
        self._sessions: dict[str, SessionState] = {}
        self._lock = asyncio.Lock()

    async def find_matching(self, messages: list[dict[str, Any]]) -> Optional[SessionState]:
        async with self._lock:
            self._evict_expired_locked()
            candidates = list(self._sessions.values())

        if not candidates or not messages:
            return None

        chain = hash_chain(messages)
        best: Optional[SessionState] = None
        for session in candidates:
            if session.checkpoint_len <= 0 or session.checkpoint_len > len(chain):
                continue
            if chain[session.checkpoint_len - 1] != session.checkpoint_hash:
                continue
            if best is None or session.checkpoint_len > best.checkpoint_len:
                best = session
        return best

    async def save(self, session: SessionState) -> None:
        session.last_used_at = time.time()
        async with self._lock:
            self._sessions[session.session_id] = session
            self._evict_expired_locked()
            overflow = len(self._sessions) - self._max_sessions
            if overflow > 0:
                oldest = sorted(self._sessions.values(), key=lambda s: s.last_used_at)
                for stale in oldest[:overflow]:
                    self._sessions.pop(stale.session_id, None)

    def _evict_expired_locked(self) -> None:
        if self._ttl <= 0:
            return
        cutoff = time.time() - self._ttl
        expired = [sid for sid, s in self._sessions.items() if s.last_used_at < cutoff]
        for sid in expired:
            self._sessions.pop(sid, None)
