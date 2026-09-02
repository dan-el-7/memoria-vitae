"""OpenAI-compatible provider for cloud vision/reasoning (OpenAI, OpenRouter,
Gemini-compat, vLLM, etc.).

Any cloud stage means data leaves the machine: callers must surface the
egress indicator (AppState.cloud_used) and record it in run metadata.

Structured vision output uses `response_format: json_schema`; if a provider
rejects that, we transparently fall back to plain JSON parsing.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx

from .base import (
    ChatMessage,
    ChatResult,
    ModelStatus,
    ProviderError,
    StreamCallback,
    ToolCall,
    ToolSpec,
)


class OpenAICompatProvider:
    kind = "openai_compat"

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key: str = "",
        num_ctx: int = 8192,
        temperature: float = 0.2,
        enable_thinking: bool = False,
        timeout_s: float = 300.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.num_ctx = num_ctx
        self.temperature = temperature
        self.enable_thinking = enable_thinking
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.AsyncClient(timeout=timeout_s, headers=headers)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = await self._client.post(f"{self.base_url}{path}", json=payload)
        except httpx.HTTPError as exc:
            raise ProviderError(f"provider {self.base_url} unreachable: {exc}") from exc
        if resp.status_code != 200:
            raise ProviderError(f"{path} -> {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    # ---------------------------------------------------------------- vision

    async def observe(self, image_jpeg: bytes, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        b64 = base64.b64encode(image_jpeg).decode("ascii")
        content = [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ]
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": content}],
            "temperature": self.temperature,
        }
        try:
            data = await self._post(
                "/v1/chat/completions",
                {
                    **payload,
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {"name": "observation", "schema": schema, "strict": True},
                    },
                },
            )
        except ProviderError:
            # Provider without json_schema support: fall back to plain JSON request.
            payload["messages"][0]["content"][0][
                "text"
            ] += "\nRespond with ONLY a JSON object matching this schema:\n" + json.dumps(schema)
            data = await self._post("/v1/chat/completions", payload)
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
        try:
            return _extract_json(text)
        except json.JSONDecodeError as exc:
            raise ProviderError(f"cloud VLM returned non-JSON: {text[:200]}") from exc

    async def inspect(self, image_jpeg: bytes, question: str) -> str:
        b64 = base64.b64encode(image_jpeg).decode("ascii")
        data = await self._post(
            "/v1/chat/completions",
            {
                "model": self.model,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": question},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    ],
                }],
                "temperature": self.temperature,
            },
        )
        return (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""

    # ------------------------------------------------------------- reasoning

    async def chat(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
        on_stream: StreamCallback | None = None,
    ) -> ChatResult:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [_to_openai(m) for m in messages],
            "temperature": self.temperature,
            "stream": bool(on_stream),
        }
        if tools:
            payload["tools"] = [
                {"type": "function",
                 "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
                for t in tools
            ]
        if not on_stream:
            data = await self._post("/v1/chat/completions", payload)
            return _result_openai(data)
        return await self._stream(payload, on_stream)

    async def _stream(self, payload: dict[str, Any], on_stream: StreamCallback) -> ChatResult:
        result = ChatResult()
        content_parts: list[str] = []
        tool_fragments: dict[int, dict[str, Any]] = {}
        usage: dict[str, Any] = {}
        try:
            async with self._client.stream("POST", f"{self.base_url}/v1/chat/completions", json=payload) as resp:
                if resp.status_code != 200:
                    body = b"".join([c async for c in resp.aiter_bytes()]).decode("utf-8", "replace")
                    raise ProviderError(f"chat -> {resp.status_code}: {body[:300]}")
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    choice = (chunk.get("choices") or [{}])[0]
                    delta = choice.get("delta") or {}
                    piece = delta.get("content") or ""
                    if piece:
                        content_parts.append(piece)
                        await on_stream(piece)
                    for tc in delta.get("tool_calls") or []:
                        idx = int(tc.get("index") or 0)
                        frag = tool_fragments.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                        fn = tc.get("function") or {}
                        frag["id"] = frag["id"] or tc.get("id") or ""
                        frag["name"] = frag["name"] or fn.get("name") or ""
                        frag["arguments"] += fn.get("arguments") or ""
                    if chunk.get("usage"):
                        usage = chunk["usage"]
        except httpx.HTTPError as exc:
            raise ProviderError(f"stream failed: {exc}") from exc
        result.content = "".join(content_parts)
        result.prompt_tokens = usage.get("prompt_tokens", 0) or 0
        result.eval_tokens = usage.get("completion_tokens", 0) or 0
        for idx in sorted(tool_fragments):
            frag = tool_fragments[idx]
            try:
                args = json.loads(frag["arguments"]) if frag["arguments"] else {}
            except json.JSONDecodeError:
                args = {}
            result.tool_calls.append(ToolCall(id=frag["id"] or f"call_{idx}", name=frag["name"], arguments=args))
        return result

    # ---------------------------------------------------------------- status

    async def status(self) -> ModelStatus:
        # Cloud models are "always loaded" from our perspective; egress applies.
        return ModelStatus(name=self.model, loaded=True, size_bytes=0, size_vram_bytes=0)

    async def preload(self) -> None:  # nothing to preload remotely
        return None

    async def unload(self) -> None:
        return None

    async def installed_models(self) -> list[dict[str, Any]]:
        try:
            resp = await self._client.get(f"{self.base_url}/v1/models")
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise ProviderError(f"provider unreachable: {exc}") from exc
        return [{"name": m.get("id"), "size_bytes": 0} for m in resp.json().get("data", [])]

    async def server_ok(self) -> bool:
        try:
            resp = await self._client.get(f"{self.base_url}/v1/models")
            return resp.status_code == 200
        except httpx.HTTPError:
            return False


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise json.JSONDecodeError("no JSON object", text, 0)
    return json.loads(text[start:end + 1])


def _to_openai(m: ChatMessage) -> dict[str, Any]:
    if m.role == "tool":
        return {"role": "tool", "tool_call_id": m.tool_name or "", "content": m.content}
    out: dict[str, Any] = {"role": m.role, "content": m.content}
    if m.tool_calls:
        out["tool_calls"] = [
            {"id": c.id or f"call_{i}", "type": "function",
             "function": {"name": c.name, "arguments": json.dumps(c.arguments or {})}}
            for i, c in enumerate(m.tool_calls)
        ]
    return out


def _result_openai(data: dict[str, Any]) -> ChatResult:
    msg = (data.get("choices") or [{}])[0].get("message") or {}
    result = ChatResult(
        content=msg.get("content") or "",
        prompt_tokens=data.get("usage", {}).get("prompt_tokens", 0) or 0,
        eval_tokens=data.get("usage", {}).get("completion_tokens", 0) or 0,
    )
    for i, tc in enumerate(msg.get("tool_calls") or []):
        fn = tc.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        result.tool_calls.append(ToolCall(id=tc.get("id") or f"call_{i}", name=fn.get("name", ""), arguments=args))
    return result
