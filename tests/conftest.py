from __future__ import annotations

import json
import uuid
from typing import Any, Optional

import httpx
import pytest
import respx

from app.config import settings


class FakeUpstream:
    """Cola de respuestas programadas para simular el upstream OpenAI-compatible.

    Cada test empuja una secuencia de respuestas con queue_completion()/queue_stream();
    el fake las devuelve en orden, una por cada request entrante, y guarda en
    `received` el payload JSON completo de cada request para poder inspeccionar
    qué se le mandó realmente al upstream (system prompt compuesto, mensajes
    acumulados en un resume, tool_choice normalizado, etc.).
    """

    def __init__(self) -> None:
        self.queue: list[dict[str, Any]] = []
        self.received: list[dict[str, Any]] = []

    def queue_completion(
        self, content: str = "", tool_calls: Optional[list[dict[str, Any]]] = None
    ) -> None:
        """Programa una respuesta no-streaming (la usa la fase de decomposición)."""
        self.queue.append({"kind": "completion", "content": content, "tool_calls": tool_calls})

    def queue_stream(
        self,
        pieces: Optional[list[str]] = None,
        tool_calls: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        """Programa una respuesta streaming (ejecución atómica / síntesis).

        pieces: fragmentos de texto emitidos como deltas de content.
        tool_calls: si se da, lista de {"id","function":{"name","arguments"}} —
        se emiten como deltas de tool_calls troceados por índice, y el stream
        termina con finish_reason="tool_calls" en vez de "stop".
        """
        self.queue.append({"kind": "stream", "pieces": pieces or [], "tool_calls": tool_calls})

    def _pop(self) -> dict[str, Any]:
        if not self.queue:
            raise AssertionError("FakeUpstream: no hay más respuestas programadas para esta request")
        return self.queue.pop(0)

    def handler(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        self.received.append(payload)
        programmed = self._pop()

        if payload.get("stream"):
            body = self._build_sse_body(programmed)
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=body
            )

        message: dict[str, Any] = {"role": "assistant", "content": programmed.get("content") or None}
        if programmed.get("tool_calls"):
            message["tool_calls"] = programmed["tool_calls"]
        return httpx.Response(
            200,
            json={
                "id": f"chatcmpl-{uuid.uuid4().hex}",
                "object": "chat.completion",
                "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
            },
        )

    def _build_sse_body(self, programmed: dict[str, Any]) -> bytes:
        lines: list[str] = []
        for piece in programmed.get("pieces", []):
            chunk = {"choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]}
            lines.append(f"data: {json.dumps(chunk)}\n\n")

        tool_calls = programmed.get("tool_calls")
        if tool_calls:
            for i, tc in enumerate(tool_calls):
                delta_tc = {
                    "index": i,
                    "id": tc.get("id"),
                    "type": "function",
                    "function": {
                        "name": tc["function"]["name"],
                        "arguments": tc["function"]["arguments"],
                    },
                }
                chunk = {
                    "choices": [
                        {"index": 0, "delta": {"tool_calls": [delta_tc]}, "finish_reason": None}
                    ]
                }
                lines.append(f"data: {json.dumps(chunk)}\n\n")
            finish_reason = "tool_calls"
        else:
            finish_reason = "stop"

        final_chunk = {"choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}]}
        lines.append(f"data: {json.dumps(final_chunk)}\n\n")
        lines.append("data: [DONE]\n\n")
        return "".join(lines).encode("utf-8")


@pytest.fixture(autouse=True)
def _fake_upstream_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Aísla los tests del contenido real de .env: apunta el upstream a un host
    ficticio que solo respx conoce, sin importar qué backend tenga configurado
    el desarrollador localmente."""
    monkeypatch.setattr(settings, "upstream_base_url", "http://fake-upstream.test")


@pytest.fixture
def fake_upstream():
    upstream = FakeUpstream()
    with respx.mock(base_url="http://fake-upstream.test", assert_all_called=False) as router:
        router.post("/v1/chat/completions").mock(side_effect=upstream.handler)
        yield upstream


@pytest.fixture(autouse=True)
def _reset_session_store():
    from app.main import session_store

    session_store._sessions.clear()
    yield
    session_store._sessions.clear()


@pytest.fixture
async def client():
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac
