"""faster-whisper STT backend (local CPU, whisper-small int8 by default).

`faster-whisper` is an OPTIONAL dependency (ctranslate2-based; brings PyAV for
audio decode). Import is lazy: the desktop runs fine without it and the voice
endpoint reports SttUnavailable with install instructions.

VAD: faster-whisper's built-in Silero VAD gate drops silence, which also
suppresses the hallucinations whisper produces on silent input.
`condition_on_previous_text=False` prevents error propagation across segments.
"""

from __future__ import annotations

import asyncio
import time

from .base import SttUnavailable, TranscriptionResult


class FasterWhisperStt:
    def __init__(self, *, model: str = "small", device: str = "cpu",
                 compute_type: str = "int8") -> None:
        self._model_name = model
        self.device = device
        self.compute_type = compute_type
        self._model: Any = None
        self._lock = asyncio.Lock()

    @property
    def model_name(self) -> str:
        return self._model_name

    def _load(self):
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise SttUnavailable(
                "faster-whisper is not installed. Run: pip install faster-whisper"
            ) from exc
        self._model = WhisperModel(
            self._model_name, device=self.device, compute_type=self.compute_type
        )
        return self._model

    async def transcribe(self, audio_bytes: bytes, *, language: str | None = None) -> TranscriptionResult:
        if not audio_bytes:
            return TranscriptionResult(model=self._model_name)
        started = time.monotonic()
        async with self._lock:  # serialize model access; VLM is never blocked
            try:
                model = await asyncio.to_thread(self._load)
                segments, info = await asyncio.to_thread(
                    model.transcribe,
                    audio_bytes,
                    language=language,
                    vad_filter=True,
                    condition_on_previous_text=False,
                    beam_size=1,
                )
                out_segments = []
                texts = []
                for seg in segments:  # generator: consumed inside the thread task
                    out_segments.append({
                        "start": round(float(seg.start), 2),
                        "end": round(float(seg.end), 2),
                        "text": seg.text.strip(),
                    })
                    texts.append(seg.text.strip())
            except SttUnavailable:
                raise
            except Exception as exc:
                raise SttUnavailable(f"transcription failed: {exc}") from exc
        return TranscriptionResult(
            text=" ".join(t for t in texts if t).strip(),
            language=getattr(info, "language", None),
            duration_s=round(float(getattr(info, "duration", 0.0) or 0.0), 2),
            segments=out_segments[:100],
            model=self._model_name,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
