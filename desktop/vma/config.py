"""Application configuration.

Loaded from `config.toml` (optional) over built-in defaults; every value that
users commonly tweak is exposed here. Nothing is hard-coded at call sites.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ProviderConfig:
    """One inference stage (vision or reasoning).

    kind: "ollama" (local) or "openai_compat" (cloud / any OpenAI-compatible API).
    """

    kind: str = "ollama"
    model: str = ""
    base_url: str = ""  # openai_compat only; ollama uses ollama_url below
    api_key: str = ""
    num_ctx: int = 4096
    keep_alive: str | int = "10m"  # duration string, or numeric -1 to pin
    num_gpu: int | None = None  # Ollama GPU layers; None=auto, lower enables hybrid CPU/GPU
    enable_thinking: bool = False
    temperature: float = 0.2
    # Ollama repeat_penalty: the small VLM has a degenerate loop mode on
    # screen-dense frames (measured: 20k chars of "485. Do not..." repetition
    # until num_ctx cuts the JSON mid-string). 1.3 on the vision stage halves
    # saturation and eliminated parse failures in testing. None = unset.
    repeat_penalty: float | None = None

    def is_cloud(self) -> bool:
        return self.kind != "ollama"


@dataclass
class PipelineConfig:
    # Change detection: frame is "changed" if either metric exceeds threshold.
    change_mad_threshold: float = 6.0  # mean absolute difference, 0-255 scale
    change_hash_threshold: int = 12  # dHash hamming distance out of 64 bits
    heartbeat_interval_s: float = 30.0  # record a heartbeat when scene is static
    # Backpressure
    intake_queue_capacity: int = 3
    min_interval_ms: int = 250
    max_interval_ms: int = 10_000
    backpressure_safety: float = 1.15  # multiplier on EMA processing time
    # Media
    save_frames: str = "important"  # "none" | "important" | "all"
    media_max_side: int = 1024  # frames are downscaled before VLM + storage
    media_jpeg_quality: int = 70
    # Retention: delete retained media files older than this many minutes
    # (0 = keep forever). Observations/DB rows are never deleted by this.
    media_retention_minutes: int = 0
    # Hard budget for retained media in bytes (0 = unlimited). When exceeded,
    # oldest non-important media is evicted first; important (>=2) is evicted
    # last and only when nothing else remains.
    media_budget_bytes: int = 0
    # Voice notes link to observations within this many minutes (temporal
    # neighbors); semantic neighbors are added via the embedding below.
    voice_note_context_minutes: int = 2
    # Hierarchical Temporal Indexing: when idle, compress each closed hour of
    # raw observations into one compact LLM timeline (hour_index table) as a
    # fast path for broad agent queries. Adds an index only — rows are never
    # merged/deleted. Skipped automatically if the reasoning stage is cloud.
    hourly_index: bool = False


@dataclass
class EmbeddingConfig:
    """Semantic embeddings of committed observations (not raw frames)."""

    enabled: bool = True
    kind: str = "ollama"  # only local ollama embeddings in v1
    model: str = "nomic-embed-text"  # pull once: `ollama pull nomic-embed-text`
    keep_alive: str | int = "10m"


@dataclass
class SttConfig:
    """Speech-to-text (push-to-talk). Continuous listening does not exist.

    Requires the optional `faster-whisper` package (CPU, int8):
      pip install faster-whisper
    Until it is installed and enabled, voice endpoints return 503.
    """

    enabled: bool = False
    model: str = "small"  # whisper small per project decision
    compute_type: str = "int8"
    device: str = "cpu"
    language: str | None = None  # None = auto-detect


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8619
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("VMA_DATA_DIR", "data")))
    # Relay / Internet pairing (M4). Empty relay_url = LAN-only mode.
    relay_url: str = ""
    relay_reg_token: str = ""


@dataclass
class AppConfig:
    ollama_url: str = "http://localhost:11434"
    vision: ProviderConfig = field(
        default_factory=lambda: ProviderConfig(
            kind="ollama",
            model="qwen3-vl:2b",
            num_ctx=4096,
            keep_alive=-1,  # numeric -1: VLM pinned; string "-1" is rejected by Ollama 0.32.x
            enable_thinking=False,
        )
    )
    reasoning: ProviderConfig = field(
        default_factory=lambda: ProviderConfig(
            kind="ollama",
            model="huihui_ai/qwen3-abliterated:8b",
            num_ctx=8192,
            keep_alive="10m",
            num_gpu=20,  # keep both models resident on an 8 GB laptop GPU via hybrid placement
            enable_thinking=False,
        )
    )
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)
    embeddings: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    stt: SttConfig = field(default_factory=SttConfig)
    server: ServerConfig = field(default_factory=ServerConfig)

    @property
    def runs_dir(self) -> Path:
        return self.server.data_dir / "runs"


def load_config(path: Path | None = None) -> AppConfig:
    """Load config.toml; unknown keys are ignored, missing keys keep defaults."""
    cfg = AppConfig()
    if path is None:
        candidates = [Path("config.toml"), Path(__file__).resolve().parent.parent / "config.toml"]
        path = next((c for c in candidates if c.exists()), None)
    if path is None or not path.exists():
        return cfg
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    for key in ("ollama_url",):
        if key in raw:
            setattr(cfg, key, raw[key])
    for section in ("vision", "reasoning"):
        if isinstance(raw.get(section), dict):
            for k, v in raw[section].items():
                if hasattr(cfg.vision, k):
                    setattr(getattr(cfg, section), k, v)
    if isinstance(raw.get("pipeline"), dict):
        for k, v in raw["pipeline"].items():
            if hasattr(cfg.pipeline, k):
                cur = getattr(cfg.pipeline, k)
                setattr(cfg.pipeline, k, Path(v) if isinstance(cur, Path) else v)
    for section in ("embeddings", "stt"):
        target = getattr(cfg, section)
        if isinstance(raw.get(section), dict):
            for k, v in raw[section].items():
                if hasattr(target, k):
                    cur = getattr(target, k)
                    setattr(target, k, Path(v) if isinstance(cur, Path) else v)
    if isinstance(raw.get("server"), dict):
        for k, v in raw["server"].items():
            if hasattr(cfg.server, k):
                cur = getattr(cfg.server, k)
                setattr(cfg.server, k, Path(v) if isinstance(cur, Path) else v)
    return cfg
