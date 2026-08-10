import json
from collections.abc import AsyncIterator
from typing import Any, Optional

import httpx

from .config import settings


class UpstreamError(Exception):
    pass


class UpstreamClient:
    def __init__(self) -> None:
        self._base_url = settings.upstream_base_url.rstrip("/")
        self._api_key = settings.resolved_api_key()

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    async def complete(
        self,
        messages: list[dict[str, Any]],
        model: str,
        json_mode: bool = False,
    ) -> str:
        message = await self.complete_raw(messages, model, json_mode=json_mode)
        return message.get("content") or ""

    async def complete_raw(
        self,
        messages: list[dict[str, Any]],
        model: str,
        json_mode: bool = False,
        tools: Optional[list[dict[str, Any]]] = None,
        tool_choice: Optional[Any] = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if tools:
            payload["tools"] = tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice

        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
            resp = await client.post(
                f"{self._base_url}/v1/chat/completions",
                headers=self._headers(),
                json=payload,
            )
        if resp.status_code >= 400:
            raise UpstreamError(f"Upstream error {resp.status_code}: {resp.text}")

        data = resp.json()
        choice = data["choices"][0]
        message = choice["message"]
        message.setdefault("finish_reason", choice.get("finish_reason"))
        return message

    async def _stream_raw_chunks(
        self,
        messages: list[dict[str, Any]],
        model: str,
        json_mode: bool = False,
        tools: Optional[list[dict[str, Any]]] = None,
        tool_choice: Optional[Any] = None,
    ) -> AsyncIterator[dict[str, Any]]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if tools:
            payload["tools"] = tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice

        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
            async with client.stream(
                "POST",
                f"{self._base_url}/v1/chat/completions",
                headers=self._headers(),
                json=payload,
            ) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
                    raise UpstreamError(f"Upstream error {resp.status_code}: {body.decode()}")

                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data_str = line[len("data:"):].strip()
                    if data_str == "[DONE]":
                        break
                    chunk = json.loads(data_str)
                    yield chunk["choices"][0]

    async def stream(
        self,
        messages: list[dict[str, Any]],
        model: str,
        json_mode: bool = False,
    ) -> AsyncIterator[str]:
        async for choice in self._stream_raw_chunks(messages, model, json_mode=json_mode):
            piece = choice.get("delta", {}).get("content")
            if piece:
                yield piece

    async def stream_raw(
        self,
        messages: list[dict[str, Any]],
        model: str,
        tools: Optional[list[dict[str, Any]]] = None,
        tool_choice: Optional[Any] = None,
    ) -> AsyncIterator[dict[str, Any]]:
        async for choice in self._stream_raw_chunks(
            messages, model, tools=tools, tool_choice=tool_choice
        ):
            yield {
                "delta": choice.get("delta", {}),
                "finish_reason": choice.get("finish_reason"),
            }
