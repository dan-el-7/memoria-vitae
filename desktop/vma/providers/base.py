"""Provider abstraction.

Two stages, each independently backed by a local or cloud provider:

  VisionProvider.observe(image, prompt, schema)   -> structured observation dict
  ReasoningProvider.chat(messages, tools, ...)    -> content / tool_calls

Providers never see anything outside the request the app builds for them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol


class ProviderError(RuntimeError):
    """Raised when a provider is unreachable or returns an unusable response."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ChatMessage:
    role: str  # "system" | "user" | "assistant" | "tool"
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_name: str | None = None  # for role == "tool"
    images: list[bytes] = field(default_factory=list)  # base64-encoded payload

    def to_plain(self) -> dict[str, Any]:
        out: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            out["tool_calls"] = [
                {"function": {"name": c.name, "arguments": c.arguments}} for c in self.tool_calls
            ]
        if self.tool_name is not None:
            out["tool_name"] = self.tool_name
        return out


@dataclass
class ChatResult:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    thinking: str = ""
    prompt_tokens: int = 0
    eval_tokens: int = 0
    total_ms: int = 0


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON schema for the arguments object


StreamCallback = Callable[[str], Awaitable[None]]


class VisionProvider(Protocol):
    async def observe(self, image_jpeg: bytes, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        """Return a JSON-schema-valid observation for the image."""
        ...

    async def inspect(self, image_jpeg: bytes, question: str) -> str:
        """Free-form re-inspection of a stored frame (the expensive path)."""
        ...


class ReasoningProvider(Protocol):
    async def chat(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
        on_stream: StreamCallback | None = None,
    ) -> ChatResult:
        ...


class EmbeddingProvider(Protocol):
    """Text embeddings for committed observations (never per-frame)."""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        ...

    @property
    def model(self) -> str:
        ...


@dataclass
class ModelStatus:
    """Runtime status of one model as reported by the backend."""

    name: str
    loaded: bool = False
    size_bytes: int = 0
    size_vram_bytes: int = 0
    expires_at: str | None = None
    quantization: str | None = None
    parameter_size: str | None = None
    error: str | None = None

    @property
    def gpu_fraction(self) -> float:
        if not self.size_bytes:
            return 0.0
        return self.size_vram_bytes / self.size_bytes

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "loaded": self.loaded,
            "size_bytes": self.size_bytes,
            "size_vram_bytes": self.size_vram_bytes,
            "gpu_fraction": round(self.gpu_fraction, 3),
            "expires_at": self.expires_at,
            "quantization": self.quantization,
            "parameter_size": self.parameter_size,
            "error": self.error,
        }
