"""Provider factory: config -> provider instances for each stage."""

from __future__ import annotations

from ..config import EmbeddingConfig, ProviderConfig
from .base import ReasoningProvider, VisionProvider
from .ollama_provider import OllamaEmbedder, OllamaProvider
from .openai_compat import OpenAICompatProvider


def make_provider(cfg: ProviderConfig, ollama_url: str) -> OllamaProvider | OpenAICompatProvider:
    if cfg.kind == "ollama":
        return OllamaProvider(
            ollama_url,
            cfg.model,
            num_ctx=cfg.num_ctx,
            keep_alive=cfg.keep_alive,
            num_gpu=cfg.num_gpu,
            enable_thinking=cfg.enable_thinking,
            temperature=cfg.temperature,
            repeat_penalty=cfg.repeat_penalty,
        )
    if cfg.kind == "openai_compat":
        if not cfg.base_url:
            raise ValueError("openai_compat provider requires base_url")
        return OpenAICompatProvider(
            cfg.base_url,
            cfg.model,
            api_key=cfg.api_key,
            num_ctx=cfg.num_ctx,
            temperature=cfg.temperature,
            enable_thinking=cfg.enable_thinking,
        )
    raise ValueError(f"unknown provider kind {cfg.kind!r}")


def make_embedder(cfg: EmbeddingConfig, ollama_url: str) -> OllamaEmbedder | None:
    """Embeddings are local-only in v1; None disables the semantic stage."""
    if not cfg.enabled:
        return None
    if cfg.kind != "ollama":
        raise ValueError(f"unknown embedding provider kind {cfg.kind!r}")
    return OllamaEmbedder(ollama_url, cfg.model, keep_alive=cfg.keep_alive)


def vision_of(provider: OllamaProvider | OpenAICompatProvider) -> VisionProvider:
    return provider  # both implement observe/inspect


def reasoning_of(provider: OllamaProvider | OpenAICompatProvider) -> ReasoningProvider:
    return provider  # both implement chat
