from __future__ import annotations

from typing import Any, Optional, Union

ContentValue = Optional[Union[str, list[dict[str, Any]]]]


def split_content(content: ContentValue) -> tuple[str, list[dict[str, Any]]]:
    """Separa un `content` de mensaje (string plano, o lista de content-parts
    al estilo OpenAI) en su texto concatenado y las partes no-texto (imágenes,
    etc.) que deban reinyectarse aparte en los mensajes que arma el motor."""
    if content is None:
        return "", []
    if isinstance(content, str):
        return content, []

    text_parts: list[str] = []
    other_parts: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            text_parts.append(part.get("text") or "")
        else:
            other_parts.append(part)
    return "".join(text_parts), other_parts


def build_multimodal_content(text: str, extra_parts: list[dict[str, Any]]) -> ContentValue:
    """Recompone un `content` de salida: string plano si no hay partes extra
    (compatibilidad total con upstreams sin soporte multimodal), o lista de
    content-parts (texto + partes) si las hay."""
    if not extra_parts:
        return text
    parts: list[dict[str, Any]] = []
    if text:
        parts.append({"type": "text", "text": text})
    parts.extend(extra_parts)
    return parts
