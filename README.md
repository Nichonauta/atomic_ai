# Atomic Decomposition Proxy

Proxy HTTP compatible con la API de OpenAI (`/v1/chat/completions`) que se coloca delante de un modelo LLM "upstream" (por defecto, DeepSeek) y, en vez de reenviar la conversación tal cual, **descompone cada instrucción en un árbol de subtareas atómicas, las resuelve una por una y luego sintetiza una respuesta final**.

La idea: modelos más pequeños o más baratos suelen fallar en tareas compuestas porque intentan resolverlo todo de un tirón. Este proxy fuerza un proceso de tres fases —planificar, ejecutar, sintetizar— para que cada paso sea lo bastante simple como para resolverse bien, manteniendo compatibilidad total con clientes que ya hablan el protocolo de OpenAI (incluye streaming SSE, `tool_calls`, contenido multimodal y `reasoning_content`).

## Cómo funciona

Cada turno del usuario pasa por tres fases, orquestadas por `AtomicDecompositionEngine` ([app/engine.py](app/engine.py)):

1. **Descomposición** — el modelo decide si la instrucción es atómica (resoluble en un solo paso) o si conviene dividirla en subtareas concretas y ordenadas. Se aplica recursivamente hasta una profundidad máxima configurable, construyendo un árbol de tareas.
2. **Ejecución de hojas atómicas** — cada tarea atómica del árbol se resuelve en su propia llamada al modelo, con el resultado de las tareas anteriores como contexto acumulado. Si el modelo necesita usar una herramienta (`tool_calls`), la ejecución se pausa y se le devuelve el `tool_calls` al cliente, tal como espera el protocolo de OpenAI.
3. **Síntesis final** — con todos los resultados atómicos ya resueltos, se genera la respuesta final que efectivamente se entrega al usuario (el resto del proceso se transmite como `reasoning_content`, no como la respuesta visible).

El detalle de cada fase (criterios de atomicidad, cómo se le explica al modelo que existen herramientas sin dárselas como ejecutables, reglas de seguridad ante prompt injection) vive en los prompts de [app/prompts/](app/prompts).

### Sesiones y pausa/reanudación

Como una tarea atómica o la síntesis pueden requerir `tool_calls`, el proxy necesita "recordar" en qué punto del árbol se quedó entre una petición HTTP y la siguiente (el cliente responde con el resultado de la herramienta en una request nueva). `SessionStore` ([app/session.py](app/session.py)) guarda ese estado en memoria, indexado por un hash encadenado del historial de mensajes, para poder:

- Reanudar exactamente donde quedó pausado, sin rehacer descomposición ni tareas ya resueltas.
- Detectar cuándo una request es un turno nuevo sobre una conversación ya completada (y sembrarlo con el resumen de turnos previos, en vez de redecomponer todo el historial crudo desde cero).
- Expirar sesiones por TTL y limitar cuántas se mantienen en memoria.

### Compatibilidad con el protocolo OpenAI

- Acepta `stream: true/false`, `tools`, `tool_choice`, contenido multimodal (texto + imágenes) y responde en el mismo formato (`chat.completion` / `chat.completion.chunk` vía SSE).
- El razonamiento interno del proxy (qué subtareas identificó, en qué va) se expone opcionalmente como `reasoning_content`, configurable con `EXPOSE_REASONING_CONTENT`.
- El `system` prompt real del caller nunca se descarta: se antepone como capa de autoridad sobre los prompts internos de cada fase.

## Estructura del proyecto

```
app/
  main.py       Endpoints FastAPI, parseo de requests, streaming SSE
  engine.py     Motor de las 3 fases (descomposición, ejecución, síntesis)
  session.py    Persistencia de sesiones en memoria (pausa/reanudación)
  upstream.py   Cliente HTTP hacia el modelo upstream (OpenAI-compatible)
  content.py    Utilidades para separar/recomponer contenido multimodal
  schemas.py    Modelos Pydantic del request/response (formato OpenAI)
  sse.py        Helpers para construir chunks de streaming SSE
  config.py     Configuración vía variables de entorno (.env)
  prompts/      Prompts de cada fase, en Markdown
tests/          Suite de pytest (unitarios + end-to-end con upstream fake)
run.py          Arranca el servidor con uvicorn
```

## Requisitos

- Python 3.9+
- Un endpoint upstream compatible con la API de chat completions de OpenAI (por defecto, DeepSeek)

## Configuración

Copia `.env.example` a `.env` y completa tus valores:

```bash
cp .env.example .env
```

Variables principales:

| Variable | Descripción | Default |
|---|---|---|
| `UPSTREAM_BASE_URL` | URL base del modelo upstream | `https://api.deepseek.com` |
| `UPSTREAM_API_KEY` | API key del upstream | *(vacío)* |
| `UPSTREAM_MODEL` | Modelo a usar si el request no especifica uno | `deepseek-v4-flash` |
| `MAX_DECOMPOSITION_DEPTH` | Profundidad máxima del árbol de subtareas | `3` |
| `MAX_TOOL_ROUNDS_PER_PHASE` | Límite de rondas de `tool_calls` por fase | `25` |
| `PROXY_HOST` / `PROXY_PORT` | Dirección donde escucha el proxy | `127.0.0.1:8000` |
| `SESSION_TTL_SECONDS` | Tiempo de vida de una sesión pausada | `1800` |
| `MAX_SESSIONS` | Máximo de sesiones en memoria | `200` |
| `EXPOSE_REASONING_CONTENT` | Si se expone el proceso interno como `reasoning_content` | `true` |

## Instalación y ejecución

```bash
python -m venv .venv
.venv\Scripts\activate      # Windows
pip install -r requirements.txt
python run.py
```

En Windows también puedes usar `run.bat`, que activa el entorno virtual y arranca el servidor.

El proxy queda disponible en `http://127.0.0.1:8000` (o el host/puerto configurado), exponiendo:

- `POST /v1/chat/completions` — endpoint principal, compatible con clientes OpenAI
- `GET /v1/models` — lista el modelo configurado
- `GET /healthz` — healthcheck

Apunta cualquier cliente compatible con la API de OpenAI (SDK oficial, agentes de código, etc.) a esta URL como `base_url`.

## Tests

```bash
pytest
```

La suite cubre el motor de descomposición, el manejo de sesiones, el contenido multimodal, los schemas y un flujo end-to-end contra un upstream simulado (`tests/test_fake_upstream.py`).
