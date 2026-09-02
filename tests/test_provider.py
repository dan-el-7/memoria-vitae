"""Provider regression tests for Ollama's qwen3-vl response shape."""

from __future__ import annotations

import pytest

from vma.providers.ollama_provider import OllamaProvider


@pytest.mark.asyncio
async def test_ollama_observe_accepts_json_in_thinking_field() -> None:
    provider = OllamaProvider("http://ollama.test", "qwen3-vl:2b", keep_alive="-1", num_gpu=20)

    async def fake_chat(payload):
        assert payload["keep_alive"] == -1
        assert payload["options"]["num_gpu"] == 20
        return {
            "message": {
                "role": "assistant",
                "content": "",
                "thinking": '{"summary":"a test scene","importance":2}',
            },
            "total_duration": 1_000_000,
        }

    provider._chat = fake_chat
    try:
        result = await provider.observe(b"jpeg", "describe", {"type": "object"})
    finally:
        await provider.aclose()

    assert result["summary"] == "a test scene"
    assert result["importance"] == 2
    assert result["_meta"]["provider"] == "ollama"


@pytest.mark.asyncio
async def test_ollama_inspect_falls_back_to_thinking_field() -> None:
    """Ollama 0.33.x can put the whole answer in `thinking` with content empty."""
    provider = OllamaProvider("http://ollama.test", "qwen3-vl:2b", keep_alive="-1", num_gpu=20)

    async def fake_chat(payload):
        return {
            "message": {"role": "assistant", "content": "", "thinking": "the answer text"},
            "total_duration": 1_000_000,
        }

    provider._chat = fake_chat
    try:
        answer = await provider.inspect(b"jpeg", "what is this?")
    finally:
        await provider.aclose()

    assert answer == "the answer text"
