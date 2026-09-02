"""Speech-to-text contracts.

Design rules (expansion spec §9):
- Runs locally on CPU, asynchronously (never blocks VLM inference).
- Push-to-talk only; continuous listening is not implemented and is OFF by design.
- STT is modular: any provider implementing SttProvider can back the endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class SttUnavailable(RuntimeError):
    """faster-whisper missing, model load failed, or STT disabled in config."""


@dataclass
class TranscriptionResult:
    text: str = ""
    language: str | None = None
    duration_s: float = 0.0
    segments: list[dict[str, Any]] = field(default_factory=list)
    model: str = ""
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "language": self.language,
            "duration_s": self.duration_s,
            "segments": self.segments,
            "model": self.model,
            "elapsed_ms": self.elapsed_ms,
        }


class SttProvider(Protocol):
    async def transcribe(self, audio_bytes: bytes, *, language: str | None = None) -> TranscriptionResult:
        """Transcribe a complete (non-streaming) recording."""
        ...

    @property
    def model_name(self) -> str:
        ...
