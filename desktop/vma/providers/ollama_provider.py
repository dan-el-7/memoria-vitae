"""Ollama provider for both vision and reasoning stages.

Verified against Ollama 0.32.x API (docs.ollama.com):

- POST /api/chat : messages with base64 `images`, `tools` (function calling),
  `format` (JSON schema structured output), `keep_alive`, `options.num_ctx`,
  and `think` for thinking-capable models.
- GET  /api/ps   : loaded models with size/size_vram/expires_at (residency).
- GET  /api/tags : installed models.

Residency contract (see docs/ollama-integration.md): every request carries an
explicit `keep_alive` from config, so model residency never depends on request
frequency and a quiet phone never unloads a model. Unloading happens only via
explicit user action, model change, or app shutdown.

Known upstream issue ollama#8095: combining `format` with `tools` in one
request can yield empty tool_calls. We therefore never combine them:
perception uses `format`, the agent loop uses `tools` (see PipelineConfig).
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from ..utils import iso
from .base import (
    ChatMessage,
    ChatResult,
    ModelStatus,
    ProviderError,
    StreamCallback,
    ToolCall,
    ToolSpec,
)

DEFAULT_TIMEOUT_S = 300.0


class OllamaProvider:
    """Talks to a local (or remote) Ollama server over HTTP."""

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        num_ctx: int = 4096,
        keep_alive: str | int = "10m",
        num_gpu: int | None = None,
        enable_thinking: bool = False,
        temperature: float = 0.2,
        repeat_penalty: float | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.num_ctx = num_ctx
        self.keep_alive = _normalize_keep_alive(keep_alive)
        self.num_gpu = num_gpu
        self.enable_thinking = enable_thinking
        self.temperature = temperature
        self.repeat_penalty = repeat_penalty
        self._client = httpx.AsyncClient(timeout=timeout_s)

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ core

    async def _chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = await self._client.post(f"{self.base_url}/api/chat", json=payload)
        except httpx.HTTPError as exc:
            raise ProviderError(f"Ollama unreachable at {self.base_url}: {exc}") from exc
        if resp.status_code != 200:
            raise ProviderError(f"Ollama /api/chat {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    def _options(self) -> dict[str, Any]:
        opts: dict[str, Any] = {"num_ctx": self.num_ctx, "temperature": self.temperature}
        if self.repeat_penalty is not None:
            opts["repeat_penalty"] = self.repeat_penalty
        if self.num_gpu is not None:
            opts["num_gpu"] = self.num_gpu
        return opts

    # --------------------------------------------------------------- vision

    async def observe(self, image_jpeg: bytes, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        """Structured perception: one image in, schema-constrained JSON out."""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": prompt,
                    "images": [_b64(image_jpeg)],
                }
            ],
            "format": schema,
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": self._options(),
            # Always disable thinking here, regardless of config: with thinking
            # enabled Ollama does not apply `format`, so the model answers in
            # chain-of-thought prose instead of schema JSON (observed with
            # qwen3-vl:2b). `enable_thinking` only governs the reasoning stage.
            "think": False,
        }
        data = await self._chat(payload)
        message = data.get("message") or {}
        # qwen3-vl on Ollama 0.32.x can place the final schema JSON in
        # `thinking` even when `think:false` is requested. Prefer content, but
        # accept that valid fallback so a successful perception is not lost.
        candidates = [message.get("content"), message.get("thinking")]
        result = None
        for candidate in candidates:
            if not isinstance(candidate, str) or not candidate.strip():
                continue
            result = _parse_json_object(candidate)
            if result is not None:
                break
        if not isinstance(result, dict):
            preview = next((c for c in candidates if isinstance(c, str) and c.strip()), "")
            raise ProviderError(f"VLM returned non-JSON content: {preview[:200]}")
        result["_meta"] = {
            "model": self.model,
            "provider": "ollama",
            "total_ms": int(data.get("total_duration", 0) / 1e6),
            "prompt_tokens": data.get("prompt_eval_count", 0),
            "eval_tokens": data.get("eval_count", 0),
        }
        return result

    async def inspect(self, image_jpeg: bytes, question: str) -> str:
        """Free-form re-inspection of a stored frame (expensive path)."""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": question, "images": [_b64(image_jpeg)]}],
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": self._options(),
            "think": False,  # re-inspection wants an answer, not deliberation
        }
        data = await self._chat(payload)
        # Same 0.33.x quirk as observe(): the answer can land in `thinking`
        # with `content` empty even when `think:false` was requested.
        message = data.get("message") or {}
        content = message.get("content") or ""
        if not content.strip():
            content = message.get("thinking") or ""
        return content

    # ------------------------------------------------------------ reasoning

    async def chat(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
        on_stream: StreamCallback | None = None,
    ) -> ChatResult:
        """Tool-calling conversation. Never sets `format` (ollama#8095)."""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [_chat_message_to_ollama(m) for m in messages],
            "stream": bool(on_stream),
            "keep_alive": self.keep_alive,
            "options": self._options(),
        }
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]
        if self.enable_thinking:
            payload["think"] = True

        if not on_stream:
            data = await self._chat(payload)
            return _result_from_response(data)

        return await self._chat_streaming(payload, on_stream)

    async def _chat_streaming(self, payload: dict[str, Any], on_stream: StreamCallback) -> ChatResult:
        result = ChatResult()
        # Accumulated per-streaming-doc: tool call fragments by index.
        fragments: dict[int, dict[str, Any]] = {}
        thinking_parts: list[str] = []
        content_parts: list[str] = []
        try:
            async with self._client.stream("POST", f"{self.base_url}/api/chat", json=payload) as resp:
                if resp.status_code != 200:
                    body = (await aread(resp)).decode("utf-8", "replace")
                    raise ProviderError(f"Ollama /api/chat {resp.status_code}: {body[:300]}")
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    msg = chunk.get("message") or {}
                    if msg.get("thinking"):
                        thinking_parts.append(msg["thinking"])
                    piece = msg.get("content") or ""
                    if piece:
                        content_parts.append(piece)
                        await on_stream(piece)
                    for call in msg.get("tool_calls") or []:
                        idx = call.get("function", {}).get("index", 0)
                        frag = fragments.setdefault(idx, {"name": "", "arguments": ""})
                        fn = call.get("function") or {}
                        if fn.get("name"):
                            frag["name"] = fn["name"]
                        if isinstance(fn.get("arguments"), str):
                            frag["arguments"] += fn["arguments"]
                        else:
                            frag["arguments"] = json.dumps(fn.get("arguments") or {})
                    if chunk.get("done"):
                        result.prompt_tokens = chunk.get("prompt_eval_count", 0) or 0
                        result.eval_tokens = chunk.get("eval_count", 0) or 0
                        result.total_ms = int(chunk.get("total_duration", 0) / 1e6)
        except httpx.HTTPError as exc:
            raise ProviderError(f"Ollama stream failed: {exc}") from exc

        result.content = "".join(content_parts)
        result.thinking = "".join(thinking_parts)
        for idx in sorted(fragments):
            frag = fragments[idx]
            try:
                args = json.loads(frag["arguments"]) if frag["arguments"] else {}
            except json.JSONDecodeError:
                args = {}
            result.tool_calls.append(ToolCall(id=f"call_{idx}", name=frag["name"], arguments=args))
        return result

    # ------------------------------------------------------------- runtime

    async def status(self) -> ModelStatus:
        """Report residency for this provider's model via /api/ps."""
        status = ModelStatus(name=self.model, error=None)
        try:
            resp = await self._client.get(f"{self.base_url}/api/ps")
            resp.raise_for_status()
            for entry in resp.json().get("models", []):
                if entry.get("name") == self.model:
                    status.loaded = True
                    status.size_bytes = entry.get("size", 0)
                    status.size_vram_bytes = entry.get("size_vram", 0)
                    status.expires_at = entry.get("expires_at")
                    details = entry.get("details") or {}
                    status.quantization = details.get("quantization_level")
                    status.parameter_size = details.get("parameter_size")
                    break
        except (httpx.HTTPError, ValueError) as exc:
            status.error = str(exc)
        return status

    async def preload(self) -> None:
        """Load the model without generating (empty messages + keep_alive)."""
        await self._chat(
            {
                "model": self.model,
                "messages": [],
                "keep_alive": _normalize_keep_alive(self.keep_alive),
                "stream": False,
                "options": self._options(),
            }
        )

    async def unload(self) -> None:
        """Explicit unload (user action / model change / shutdown only)."""
        await self._chat({"model": self.model, "messages": [], "keep_alive": 0, "stream": False})

    async def installed_models(self) -> list[dict[str, Any]]:
        """Installed models for UI pickers: name, size, capabilities."""
        try:
            resp = await self._client.get(f"{self.base_url}/api/tags")
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise ProviderError(f"Ollama unreachable: {exc}") from exc
        out = []
        for m in resp.json().get("models", []):
            out.append(
                {
                    "name": m.get("name"),
                    "size_bytes": m.get("size", 0),
                    "capabilities": (m.get("details") or {}).get("families"),
                    "parameter_size": (m.get("details") or {}).get("parameter_size"),
                    "quantization": (m.get("details") or {}).get("quantization_level"),
                    "digest": m.get("digest", "")[:12],
                    "modified_at": m.get("modified_at"),
                }
            )
        return out

    async def server_ok(self) -> bool:
        try:
            resp = await self._client.get(f"{self.base_url}/api/version")
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def last_used(self) -> str:
        return iso()


def _b64(data: bytes) -> str:
    import base64

    return base64.b64encode(data).decode("ascii")


def _normalize_keep_alive(value: str | int) -> str | int:
    """Ollama 0.32.x requires pin/unload sent as numeric values, not strings."""
    if isinstance(value, str) and value.strip() in {"-1", "0"}:
        return int(value.strip())
    return value


def _parse_json_object(text: str) -> dict[str, Any] | None:
    """Parse direct or fenced/explanatory JSON emitted by a VLM."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].lstrip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    try:
        value = json.loads(cleaned)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            value = json.loads(cleaned[start:end + 1])
            return value if isinstance(value, dict) else None
        except json.JSONDecodeError:
            return None


def _chat_message_to_ollama(m: ChatMessage) -> dict[str, Any]:
    out: dict[str, Any] = {"role": m.role, "content": m.content}
    if m.images:
        out["images"] = m.images
    if m.tool_calls:
        out["tool_calls"] = [
            {"function": {"name": c.name, "arguments": c.arguments}} for c in m.tool_calls
        ]
    if m.role == "tool" and m.tool_name:
        out["tool_name"] = m.tool_name
    return out


def _result_from_response(data: dict[str, Any]) -> ChatResult:
    msg = data.get("message") or {}
    result = ChatResult(
        content=msg.get("content", ""),
        thinking=msg.get("thinking", ""),
        prompt_tokens=data.get("prompt_eval_count", 0) or 0,
        eval_tokens=data.get("eval_count", 0) or 0,
        total_ms=int(data.get("total_duration", 0) / 1e6),
    )
    for call in msg.get("tool_calls") or []:
        fn = call.get("function") or {}
        args = fn.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        result.tool_calls.append(
            ToolCall(id=str(call.get("id") or fn.get("name") or ""), name=fn.get("name", ""), arguments=args)
        )
    return result


async def aread(resp: httpx.Response) -> bytes:
    chunks = []
    async for chunk in resp.aiter_bytes():
        chunks.append(chunk)
    return b"".join(chunks)


class OllamaEmbedder:
    """Local text embeddings via Ollama /api/embed (batch, one call per batch).

    Used once per COMMITTED observation — never per received frame. Older
    Ollama builds without /api/embed fall back to the single-input
    /api/embeddings endpoint.
    """

    def __init__(self, base_url: str, model: str, *, keep_alive: str | int = "10m",
                 timeout_s: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._model = model
        self.keep_alive = _normalize_keep_alive(keep_alive)
        self._client = httpx.AsyncClient(timeout=timeout_s)

    @property
    def model(self) -> str:
        return self._model

    async def aclose(self) -> None:
        await self._client.aclose()

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        payload = {
            "model": self._model,
            "input": texts,
            "keep_alive": self.keep_alive,
        }
        try:
            resp = await self._client.post(f"{self.base_url}/api/embed", json=payload)
        except httpx.HTTPError as exc:
            raise ProviderError(f"Ollama unreachable at {self.base_url}: {exc}") from exc
        if resp.status_code == 404:
            return [await self._embed_single(t) for t in texts]
        if resp.status_code != 200:
            raise ProviderError(
                f"Ollama /api/embed {resp.status_code}: {resp.text[:200]} "
                f"(is the model pulled? ollama pull {self._model})"
            )
        data = resp.json()
        embeddings = data.get("embeddings") or []
        if len(embeddings) != len(texts):
            raise ProviderError("Ollama /api/embed returned a mismatched batch")
        return [list(map(float, e)) for e in embeddings]

    async def _embed_single(self, text: str) -> list[float]:
        try:
            resp = await self._client.post(
                f"{self.base_url}/api/embeddings",
                json={"model": self._model, "prompt": text, "keep_alive": self.keep_alive},
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"Ollama unreachable at {self.base_url}: {exc}") from exc
        if resp.status_code != 200:
            raise ProviderError(f"Ollama /api/embeddings {resp.status_code}: {resp.text[:200]}")
        return [float(x) for x in resp.json().get("embedding") or []]
