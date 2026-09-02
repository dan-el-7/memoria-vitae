"""Modular local speech-to-text (push-to-talk).

Public surface: SttUnavailable, TranscriptionResult, SttProvider, FasterWhisperStt.
Wired in app.py via SttConfig; disabled unless config `[stt] enabled = true`
AND the optional `faster-whisper` package is installed.
"""

from __future__ import annotations

from .base import SttProvider, SttUnavailable, TranscriptionResult
from .faster_whisper_stt import FasterWhisperStt

__all__ = ["SttProvider", "SttUnavailable", "TranscriptionResult", "FasterWhisperStt"]
