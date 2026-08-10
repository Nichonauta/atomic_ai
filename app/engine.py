from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from . import prompts
from .config import settings
from .content import build_multimodal_content
from .upstream import UpstreamClient

Event = tuple[str, Any]  # ("reasoning" | "content", text) | ("tool_calls", list[dict])
Phase = Literal["leaf", "synthesis"]


def _merge_tool_call_delta(acc: dict[int, dict], deltas: list[dict]) -> None:
    for d in deltas:
        idx = d.get("index", 0)
        entry = acc.setdefault(
            idx, {"id": None, "type": "function", "function": {"name": "", "arguments": ""}}
        )
        if d.get("id"):
            entry["id"] = d["id"]
        if d.get("type"):
            entry["type"] = d["type"]
        fn = d.get("function") or {}
        if fn.get("name"):
            entry["function"]["name"] += fn["name"]
        if fn.get("arguments"):
            entry["function"]["arguments"] += fn["arguments"]


@dataclass
class TaskNode:
    description: str
    depth: int
    children: list["TaskNode"] = field(default_factory=list)
    is_atomic: bool = False
    result: str | None = None


@dataclass
class GoalContext:
    """Resultado de separar una request entrante en sus partes con distinto
    rol: el system prompt del caller (autoridad real, no dato a clasificar),
    la instrucción del turno actual (lo único que se decompone), el contexto
    de turnos previos (fondo, nunca redecompuesto) y las partes de imagen
    encontradas en cualquier mensaje."""

    caller_system: str
    turn_instruction: str
    prior_context: str
    image_parts: list[dict[str, Any]] = field(default_factory=list)


def _parse_decomposition(raw: str) -> dict:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    return {"atomic": True, "subtasks": []}


def _collect_atomic_leaves(node: TaskNode) -> list[TaskNode]:
    if node.is_atomic:
        return [node]
    leaves: list[TaskNode] = []
    for child in node.children:
        leaves.extend(_collect_atomic_leaves(child))
    return leaves


def _render_tree(node: TaskNode, depth: int = 0) -> list[str]:
    lines: list[str] = []
    for child in node.children:
        marker = " (atómica)" if child.is_atomic else ""
        lines.append("  " * depth + f"- {child.description}{marker}")
        lines.extend(_render_tree(child, depth + 1))
    return lines


class AtomicDecompositionEngine:
    def __init__(
        self,
        client: UpstreamClient,
        model: str,
        tools: list[dict] | None = None,
        tool_choice: Any = None,
    ) -> None:
        self._client = client
        self._model = model
        self._tools = tools
        self._tool_choice = tool_choice

        # goal_ctx se asigna una vez por turno externo (ver app/main.py), antes
        # de llamar a run()/resume() — es invariante durante todo ese run, por
        # eso vive como atributo en vez de pasarse por parámetro a cada método.
        self.goal_ctx: GoalContext | None = None

        # Estado del árbol de tareas y de la ejecución, expuesto como atributos
        # públicos para que una sesión persistida pueda restaurarlo entre
        # peticiones HTTP distintas (ver app/session.py) sin rehacer trabajo ya
        # completado.
        self.root: TaskNode | None = None
        self.leaves: list[TaskNode] = []
        self.results: list[str] = []

        # Estado de pausa/reanudación, unificado entre hoja atómica y síntesis
        # final (ambas pueden disparar tool calls y ambas deben poder
        # reanudarse con el historial completo de rondas ya vistas).
        self.pending_phase: Optional[Phase] = None
        self.pending_leaf_index: Optional[int] = None
        self.pending_tool_calls: list[dict[str, Any]] = []
        self.pending_conversation: list[dict[str, Any]] = []
        self.tool_round_count: int = 0

    # ------------------------------------------------------------------
    # Helpers de composición de prompts
    # ------------------------------------------------------------------

    def _compose_system(self, phase_prompt: str) -> str:
        """Antepone el system prompt real del caller (si lo hay) como una capa
        de autoridad sobre el prompt interno de esta fase, en vez de
        sustituirlo — así el caller (p. ej. un agente de código con sus
        propias convenciones) no pierde el control de cómo debe comportarse
        el modelo en cada llamada interna del proxy."""
        caller_system = self.goal_ctx.caller_system if self.goal_ctx else ""
        if not caller_system:
            return phase_prompt
        preamble = prompts.CALLER_SYSTEM_PREAMBLE.format(caller_system=caller_system)
        return f"{preamble}\n\n{phase_prompt}"

    def _build_user_content(self, text: str) -> str | list[dict[str, Any]]:
        image_parts = self.goal_ctx.image_parts if self.goal_ctx else []
        return build_multimodal_content(text, image_parts)

    def _tools_summary(self) -> str:
        """Descripción textual (no funcional) de las tools disponibles, para
        que la Fase 1 sepa que existen sin recibirlas como tools reales —así
        evita el defecto de clasificar como "no atómica" cualquier tarea que
        dependa de un dato que una sola invocación de herramienta resolvería
        (ver <herramientas_y_atomicidad> en decomposition_system.md)."""
        if not self._tools:
            return "(ninguna herramienta disponible)"
        lines = []
        for tool in self._tools:
            fn = tool.get("function", {})
            name = fn.get("name", "?")
            description = fn.get("description", "")
            lines.append(f"- {name}: {description}" if description else f"- {name}")
        return "\n".join(lines)

    def _normalize_tool_choice(self) -> tuple[list[dict[str, Any]] | None, Any]:
        """Política única de tools/tool_choice para todas las llamadas
        internas del motor: si el caller no dio tools (o las desactivó con
        "none"), no se ofrecen; si las dio, siempre se ofrecen en modo "auto"
        para las llamadas internas — nunca se fuerza aquí un tool_choice
        específico que el caller haya pedido para su propia respuesta final
        (forzar una función concreta en cada tarea atómica interna no tiene
        sentido semántico)."""
        if not self._tools or self._tool_choice == "none":
            return None, None
        return self._tools, "auto"

    def _enter_pending(self, phase: Phase, leaf_index: Optional[int], payload: dict[str, Any]) -> None:
        self.tool_round_count += 1
        self.pending_conversation.append(
            {"role": "assistant", "content": payload["content"], "tool_calls": payload["tool_calls"]}
        )
        self.pending_tool_calls = payload["tool_calls"]
        self.pending_phase = phase
        self.pending_leaf_index = leaf_index

    def _clear_pending(self) -> None:
        self.pending_phase = None
        self.pending_conversation = []
        self.pending_tool_calls = []
        self.pending_leaf_index = None
        self.tool_round_count = 0

    # ------------------------------------------------------------------
    # Fase 1: descomposición
    # ------------------------------------------------------------------

    async def _decompose_node(self, node: TaskNode) -> AsyncIterator[Event]:
        if node.depth >= settings.max_decomposition_depth:
            node.is_atomic = True
            return

        if node.depth == 0:
            yield ("reasoning", "Pensando en las subtareas iniciales.\n\n")
        else:
            yield ("reasoning", f"Ahora para la subtarea {node.description}, hago sus subtareas.\n\n")

        system = self._compose_system(prompts.DECOMPOSITION_SYSTEM_PROMPT)
        user_text = prompts.DECOMPOSITION_USER_PROMPT.format(
            goal=self.goal_ctx.turn_instruction,
            prior_context=self.goal_ctx.prior_context or "(sin contexto previo)",
            tools=self._tools_summary(),
            task=node.description,
        )
        # La Fase 1 nunca recibe tools *funcionales* (no puede invocarlas): mezclar
        # response_format=json_object con tool-calling es frágil entre proveedores
        # distintos. Pero SÍ recibe su descripción como texto (_tools_summary), para
        # que sepa que existen y no fragmente en pasos una tarea que una sola
        # invocación de herramienta resolvería en Fase 2.
        raw = await self._client.complete(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": self._build_user_content(user_text)},
            ],
            model=self._model,
            json_mode=True,
        )

        parsed = _parse_decomposition(raw)
        subtasks = parsed.get("subtasks") or []

        if parsed.get("atomic", True) or not subtasks:
            yield ("reasoning", "Es atómica, no se subdivide más.\n\n")
            node.is_atomic = True
            return

        subtasks_list = "\n".join(f"- {s}" for s in subtasks)
        yield ("reasoning", f"Subtareas:\n{subtasks_list}\n\n")

        for sub in subtasks:
            child = TaskNode(description=sub, depth=node.depth + 1)
            node.children.append(child)
            async for event in self._decompose_node(child):
                yield event

    async def build_task_tree(self) -> AsyncIterator[Event]:
        assert self.goal_ctx is not None
        self.root = TaskNode(description=self.goal_ctx.turn_instruction, depth=0)
        async for event in self._decompose_node(self.root):
            yield event

    # ------------------------------------------------------------------
    # Motor de streaming compartido por hojas atómicas y síntesis
    # ------------------------------------------------------------------

    async def _run_phase(
        self,
        system: str,
        user_text: str,
        emit_kind: Literal["reasoning", "content"],
        extra_messages: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[Event]:
        """Ejecuta una sola ronda de streaming para la fase en curso (hoja
        atómica o síntesis). No sabe cuál de las dos es — solo construye
        mensajes, aplica la política de tools/tool_choice y el límite de
        rondas, y emite ("_tool_calls_pending", {content, tool_calls}) o
        ("_phase_done", texto). La orquestación de qué hacer con eso (guardar
        resultado de hoja, marcar pendiente, seguir con la siguiente hoja...)
        la hacen _execute_from/resume_phase/synthesize_final."""
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": self._build_user_content(user_text)},
        ]
        if extra_messages:
            messages.extend(extra_messages)

        phase_tools, phase_tool_choice = self._normalize_tool_choice()
        if phase_tools and self.tool_round_count >= settings.max_tool_rounds_per_phase:
            phase_tools, phase_tool_choice = None, None
            yield (
                emit_kind,
                "\n\n[límite de rondas de herramientas alcanzado, respondo con lo que hay disponible]\n\n",
            )

        result_parts: list[str] = []
        tool_call_acc: dict[int, dict] = {}
        async for chunk in self._client.stream_raw(
            messages, model=self._model, tools=phase_tools, tool_choice=phase_tool_choice
        ):
            delta = chunk["delta"]
            piece = delta.get("content")
            if piece:
                result_parts.append(piece)
                yield (emit_kind, piece)
            if delta.get("tool_calls"):
                _merge_tool_call_delta(tool_call_acc, delta["tool_calls"])

        result_text = "".join(result_parts)

        if tool_call_acc:
            tool_calls = [tool_call_acc[i] for i in sorted(tool_call_acc)]
            if emit_kind == "reasoning":
                yield (
                    "reasoning",
                    "\n\nSe requiere usar una herramienta antes de continuar; interrumpo el resto del proceso.\n\n",
                )
            yield ("_tool_calls_pending", {"content": result_text or None, "tool_calls": tool_calls})
            return

        if emit_kind == "reasoning":
            yield ("reasoning", "\n\n")
        yield ("_phase_done", result_text)

    # ------------------------------------------------------------------
    # Fase 2: ejecución de hojas atómicas
    # ------------------------------------------------------------------

    def _leaf_phase_inputs(self, leaf: TaskNode) -> tuple[str, str]:
        assert self.goal_ctx is not None
        system = self._compose_system(prompts.EXECUTE_ATOMIC_SYSTEM_PROMPT)
        context = "\n".join(self.results) if self.results else "(ninguno todavía)"
        user_text = prompts.EXECUTE_ATOMIC_USER_PROMPT.format(
            goal=self.goal_ctx.turn_instruction,
            prior_context=self.goal_ctx.prior_context or "(sin contexto previo)",
            context=context,
            task=leaf.description,
        )
        return system, user_text

    async def _execute_from(self, start_index: int) -> AsyncIterator[Event]:
        total = len(self.leaves)
        for i in range(start_index, total):
            leaf = self.leaves[i]
            label = leaf.description if leaf.depth > 0 else "la solicitud"
            yield ("reasoning", f"Tarea atómica {i + 1}/{total}: {label}\n\n")

            system, user_text = self._leaf_phase_inputs(leaf)
            self.tool_round_count = 0
            self.pending_conversation = []

            async for kind, payload in self._run_phase(system, user_text, "reasoning"):
                if kind == "_tool_calls_pending":
                    self._enter_pending("leaf", i, payload)
                    yield ("tool_calls", payload["tool_calls"])
                    return
                if kind == "_phase_done":
                    leaf.result = payload
                    self.results.append(f"- {leaf.description}:\n{payload}")
                    continue
                yield (kind, payload)

    async def execute_tree(self) -> AsyncIterator[Event]:
        self.results = []
        self.leaves = _collect_atomic_leaves(self.root)
        total = len(self.leaves)
        yield ("reasoning", f"Fase 2 de 3. Implemento las {total} tareas atómicas.\n\n")
        async for event in self._execute_from(0):
            yield event

    # ------------------------------------------------------------------
    # Reanudación unificada (hoja o síntesis) tras resultados de tool
    # ------------------------------------------------------------------

    async def resume_phase(self, tool_outputs: dict[str, str]) -> AsyncIterator[Event]:
        """Continúa la fase que quedó pausada esperando resultados de tool,
        agregando la nueva ronda a self.pending_conversation (append-only) en
        vez de reconstruirla desde cero — así una segunda (o tercera...) ronda
        de tool calls dentro de la misma fase no pierde el historial de las
        rondas anteriores. Funciona igual para una hoja atómica que para la
        síntesis final."""
        assert self.pending_phase is not None
        phase = self.pending_phase
        index = self.pending_leaf_index  # solo válido si phase == "leaf"

        for tc in self.pending_tool_calls:
            self.pending_conversation.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.get("id"),
                    "content": tool_outputs.get(tc.get("id"), ""),
                }
            )

        if phase == "leaf":
            assert index is not None
            leaf = self.leaves[index]
            system, user_text = self._leaf_phase_inputs(leaf)
            emit_kind: Literal["reasoning", "content"] = "reasoning"
        else:
            system, user_text = self._synthesis_phase_inputs()
            emit_kind = "content"

        extra_messages = list(self.pending_conversation)
        async for kind, payload in self._run_phase(system, user_text, emit_kind, extra_messages=extra_messages):
            if kind == "_tool_calls_pending":
                self._enter_pending(phase, index, payload)
                yield ("tool_calls", payload["tool_calls"])
                return
            if kind == "_phase_done":
                self._clear_pending()
                if phase == "leaf":
                    leaf.result = payload
                    self.results.append(f"- {leaf.description}:\n{payload}")
                continue
            yield (kind, payload)

        if phase == "leaf":
            async for event in self._execute_from(index + 1):
                yield event

    # ------------------------------------------------------------------
    # Fase 3: síntesis final
    # ------------------------------------------------------------------

    def _synthesis_phase_inputs(self) -> tuple[str, str]:
        assert self.goal_ctx is not None
        system = self._compose_system(prompts.SYNTHESIS_SYSTEM_PROMPT)
        context = "\n".join(self.results) if self.results else "(sin resultados)"
        user_text = prompts.SYNTHESIS_USER_PROMPT.format(
            goal=self.goal_ctx.turn_instruction,
            prior_context=self.goal_ctx.prior_context or "(sin contexto previo)",
            context=context,
        )
        return system, user_text

    async def synthesize_final(self) -> AsyncIterator[Event]:
        system, user_text = self._synthesis_phase_inputs()
        self.tool_round_count = 0
        self.pending_conversation = []

        async for kind, payload in self._run_phase(system, user_text, "content"):
            if kind == "_tool_calls_pending":
                self._enter_pending("synthesis", None, payload)
                yield ("tool_calls", payload["tool_calls"])
                return
            if kind == "_phase_done":
                continue  # el texto final ya se emitió en streaming como "content"
            yield (kind, payload)

    # ------------------------------------------------------------------
    # Orquestación de alto nivel
    # ------------------------------------------------------------------

    async def run(self) -> AsyncIterator[Event]:
        yield ("reasoning", "Fase 1 de 3. Primero comienzo dividiendo la tarea en sus subtareas atómicas.\n\n")
        async for event in self.build_task_tree():
            yield event
        yield ("reasoning", "Listo, tenemos la lista completa del árbol de tareas hasta sus subtareas atómicas.\n\n")
        tree_lines = _render_tree(self.root)
        if tree_lines:
            yield ("reasoning", "\n".join(tree_lines) + "\n\n")

        tool_break = False
        async for event in self.execute_tree():
            if event[0] == "tool_calls":
                tool_break = True
            yield event
        if tool_break:
            return

        yield (
            "reasoning",
            "Fase 3 de 3. Listo, todas las tareas atómicas trabajadas correctamente, procedo a dar la respuesta final.\n",
        )
        async for event in self.synthesize_final():
            yield event

    async def resume(self, tool_outputs: dict[str, str]) -> AsyncIterator[Event]:
        """Reanuda un run() previamente pausado por una tool call (en una hoja
        o en la síntesis), sin volver a descomponer el objetivo ni reejecutar
        trabajo ya resuelto."""
        resuming_phase = self.pending_phase
        tool_break = False
        async for event in self.resume_phase(tool_outputs):
            if event[0] == "tool_calls":
                tool_break = True
            yield event
        if tool_break:
            return

        if resuming_phase == "synthesis":
            return  # resume_phase ya completó la síntesis, no hay nada más

        yield (
            "reasoning",
            "Fase 3 de 3. Listo, todas las tareas atómicas trabajadas correctamente, procedo a dar la respuesta final.\n",
        )
        async for event in self.synthesize_final():
            yield event
