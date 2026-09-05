"""Pipeline worker: the single consumer loop of the perception stage.

frame -> change detection -> (nochange: heartbeat bookkeeping) -> VLM ->
importance check -> store (+ optional media retention) -> metrics/backpressure.

Throughput (EMA of VLM latency) drives `recommended_interval_ms`, which the
transport layer forwards to the phone. The loop keeps running regardless of
phone connectivity: a disconnect drains the queue, then the worker idles while
models stay resident (unloading is never tied to connection state).
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Awaitable

from PIL import Image

from ..config import PipelineConfig
from ..providers.base import VisionProvider
from ..store.db import RunStore
from ..utils import clamp, ema, iso, parse_iso, utcnow_minus
from .change import ChangeDetector
from .intake import Frame, FrameIntake
from .perceive import flatten_observation, issue_count, perceive


@dataclass
class WorkerStatus:
    running: bool = False
    vlm_latency_ms_ema: float | None = None
    last_vlm_ms: float | None = None
    recommended_interval_ms: int = 1000
    fps: float = 0.0
    last_observation: str | None = None
    last_observation_ts: str | None = None
    last_heartbeat_ts: str | None = None
    processed_total: int = 0
    last_error: str | None = None
    embeddings_enabled: bool | None = None  # None = unknown yet, False = breaker open

    def to_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "vlm_latency_ms_ema": round(self.vlm_latency_ms_ema, 1) if self.vlm_latency_ms_ema else None,
            "vlm_ms_last": round(self.last_vlm_ms) if self.last_vlm_ms else None,
            "recommended_interval_ms": self.recommended_interval_ms,
            "fps": round(self.fps, 3),
            "last_observation": self.last_observation,
            "last_observation_ts": self.last_observation_ts,
            "last_heartbeat_ts": self.last_heartbeat_ts,
            "processed_total": self.processed_total,
            "last_error": self.last_error,
            "embeddings_enabled": self.embeddings_enabled,
        }


@dataclass
class FrameAck:
    seq: int
    verdict: str  # accepted|nochange|stale_dropped|duplicate|error
    recommended_interval_ms: int
    queue_depth: int
    detail: str = ""


AckCallback = Callable[[FrameAck], Awaitable[None]]


def _top_confidence(vlm: dict[str, Any]) -> float | None:
    """Highest observation confidence, for the list/summary views."""
    confs = [
        o.get("confidence")
        for o in vlm.get("observations") or []
        if isinstance(o, dict) and isinstance(o.get("confidence"), (int, float))
    ]
    return max(confs) if confs else None


def _ts_gap_seconds(a: str, b: str) -> float | None:
    """Seconds from ISO timestamp a to b (None if either is unparseable)."""
    try:
        return (parse_iso(b) - parse_iso(a)).total_seconds()
    except (ValueError, TypeError):
        return None


def _dominant_scene(scenes: list[str]) -> str | None:
    """Most common non-empty scene label in an event (ties: first seen)."""
    counts: dict[str, int] = {}
    for s in scenes:
        if s:
            counts[s] = counts.get(s, 0) + 1
    return max(counts, key=counts.get) if counts else None


class PipelineWorker:
    def __init__(
        self,
        vision: VisionProvider,
        store: RunStore,
        pipeline_cfg: PipelineConfig,
        run_dir: Path,
        *,
        device_id: str = "unknown",
        embedder: Any | None = None,
        indexer: Any | None = None,  # HourlyIndexer (HTI fast path), optional
    ) -> None:
        self.vision = vision
        self.store = store
        self.cfg = pipeline_cfg
        self.run_dir = run_dir
        self.device_id = device_id
        self.embedder = embedder
        self.indexer = indexer
        self.intake = FrameIntake(capacity=pipeline_cfg.intake_queue_capacity)
        self.detector = ChangeDetector(
            mad_threshold=pipeline_cfg.change_mad_threshold,
            hash_threshold=pipeline_cfg.change_hash_threshold,
        )
        self.status = WorkerStatus()
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._net_latency_ema: float | None = None  # ms, measured at transport
        self._last_heartbeat_mono: float = 0.0
        self._prev_summary: str | None = None
        self._media_dir = run_dir / "media"
        self._embed_failures = 0  # circuit breaker: 3 strikes disables embeddings
        self._last_media_sweep_mono = 0.0
        self._last_index_sweep_mono = 0.0
        self._index_task: asyncio.Task[None] | None = None
        self._event: dict[str, Any] | None = None  # open event segment (see _track_event)

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self.status.running = True
            self._task = asyncio.create_task(self._run(), name="pipeline-worker")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            self._task = None
        if self._index_task is not None and not self._index_task.done():
            self._index_task.cancel()
        self._index_task = None
        try:
            self._flush_event()  # close the open event segment
        except Exception:
            pass
        self.status.running = False

    # --------------------------------------------------------------- input

    async def submit(self, frame: Frame) -> FrameAck:
        """Enqueue one frame; returns an immediate ack with backpressure info."""
        t0 = time.monotonic()
        frame.received_ms = t0
        verdict = await self.intake.put(frame)
        if verdict == "duplicate":
            # Replayed seq (reconnect buffer / watchdog resend): acked so the
            # stop-and-wait phone advances, but recorded so run stats reconcile
            # with the phone's frames-sent counter.
            self.store.add_metric("frame_duplicate", 1, {"seq": frame.seq})
        net_ms = (time.monotonic() - t0) * 1000
        self._net_latency_ema = ema(self._net_latency_ema, net_ms, alpha=0.2)
        return FrameAck(
            seq=frame.seq,
            verdict=verdict,
            recommended_interval_ms=self.status.recommended_interval_ms,
            queue_depth=self.intake.qsize(),
            detail=f"intake={verdict}",
        )

    def set_network_latency(self, ms: float) -> None:
        """Transport can feed measured RTT here to refine the recommendation."""
        self._net_latency_ema = ema(self._net_latency_ema, ms, alpha=0.2)

    # ---------------------------------------------------------------- loop

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                frame = await asyncio.wait_for(self.intake.get(), timeout=1.0)
            except asyncio.TimeoutError:
                self._periodic_media_sweep()
                self._periodic_index_sweep()
                await self._maybe_heartbeat()
                continue
            except asyncio.CancelledError:
                return
            try:
                await self._process(frame)
            except Exception as exc:  # keep the worker alive no matter what
                self.status.last_error = str(exc)[:200]
                self.store.add_metric("worker_error", 1, {"error": str(exc), "seq": frame.seq})
                await self._maybe_heartbeat()

    async def _process(self, frame: Frame) -> None:
        changed, mad, dist = self.detector.evaluate(frame.jpeg)

        if not changed:
            self.store.add_frame(
                seq=frame.seq, ts_server=iso(), ts_device=frame.ts_device,
                width=frame.width, height=frame.height, gps=frame.gps,
                change_score=mad, hash_distance=dist, verdict="nochange",
            )
            if frame.gps and frame.gps.get("lat") is not None:
                self.store.add_location(
                    frame.ts_device or iso(), frame.gps["lat"], frame.gps["lon"],
                    frame.gps.get("accuracy_m"), frame.gps.get("speed_mps"), source="frame",
                )
            await self._maybe_heartbeat()
            self.status.processed_total += 1
            return

        # Record the accepted frame row first (dedup contract), then perceive.
        frame_id = self.store.add_frame(
            seq=frame.seq, ts_server=iso(), ts_device=frame.ts_device,
            width=frame.width, height=frame.height, gps=frame.gps,
            change_score=mad, hash_distance=dist, verdict="accepted",
        )
        if frame_id is None:
            return  # duplicate seq raced through; already stored

        t0 = time.monotonic()
        context = {
            "ts_device": frame.ts_device,
            "seq": frame.seq,
            "device_id": self.device_id,
            "gps": frame.gps,
            "previous_summary": self._prev_summary,
        }
        try:
            result = await perceive(self.vision, frame.jpeg, context=context)
        except Exception as exc:
            self.status.last_error = f"perceive: {str(exc)[:180]}"
            self.store.set_frame_verdict(frame_id, "error", model=str(exc)[:120])
            self.store.add_metric("perceive_error", 1, {"seq": frame.seq, "error": str(exc)[:300]})
            return
        latency_ms = int((time.monotonic() - t0) * 1000)
        self.status.last_vlm_ms = float(latency_ms)

        payload = result.payload
        vlm = payload.get("vlm") or {}
        importance = int(vlm.get("importance", 1))
        obs_id = self.store.add_observation(
            frame_id=frame_id,
            ts=payload.get("timestamp") or iso(),
            kind="scene",
            scene=vlm.get("scene"),
            summary=vlm.get("summary", ""),
            payload=payload,
            importance=importance,
            importance_reason=vlm.get("importance_reason"),
            confidence=_top_confidence(vlm),
            model=result.model,
            provider=result.provider,
            latency_ms=latency_ms,
        )
        self.store.set_frame_verdict(frame_id, "accepted", vlm_latency_ms=latency_ms, model=result.model)

        if frame.gps and frame.gps.get("lat") is not None:
            self.store.add_location(
                payload.get("timestamp") or iso(), frame.gps["lat"], frame.gps["lon"],
                frame.gps.get("accuracy_m"), frame.gps.get("speed_mps"), source="frame",
            )

        if self._should_save_frame(importance):
            self._save_media(frame_id, obs_id, frame.jpeg, importance)
            self._enforce_media_budget()

        # Derived, additive event tracking (scene runs split by time gaps).
        self._track_event(obs_id, payload.get("timestamp") or iso(), vlm.get("scene"), importance)

        # Embedding runs once per COMMITTED observation — never per frame.
        await self._embed_observation(obs_id, payload)

        # Metrics + backpressure
        self.status.vlm_latency_ms_ema = ema(self.status.vlm_latency_ms_ema, latency_ms, alpha=0.3)
        self._update_recommended_interval()
        self.store.add_metric("vlm_latency_ms", latency_ms, {"seq": frame.seq, "obs_id": obs_id})
        self.store.add_metric("queue_depth", self.intake.qsize())

        self._prev_summary = flatten_observation(payload)
        self.status.last_observation = self._prev_summary
        self.status.last_observation_ts = payload.get("timestamp")
        self.status.processed_total += 1
        self.status.last_error = None  # a healthy frame clears the UI ERROR chip
        self._last_heartbeat_mono = time.monotonic()

    # ----------------------------------------------------------- internals

    # ---- event segmentation (derived, additive) ----

    def track_observation(self, obs_id: int, ts: str, scene: str | None,
                          importance: int) -> None:
        """Public hook so non-frame commits (voice notes) join events too."""
        self._track_event(obs_id, ts, scene, importance)

    def flush_event(self) -> None:
        self._flush_event()

    def _track_event(self, obs_id: int, ts: str, scene: str | None, importance: int) -> None:
        if self._event is None:
            self._event = {"start": ts, "last": ts, "n": 1,
                           "scenes": [scene] if scene else [],
                           "rep": (obs_id, importance)}
            return
        gap = _ts_gap_seconds(self._event["last"], ts)
        duration = _ts_gap_seconds(self._event["start"], ts)
        if (gap is None or gap > self.cfg.event_gap_minutes * 60
                or duration is None or duration > self.cfg.event_max_minutes * 60):
            self._flush_event()
            self._event = {"start": ts, "last": ts, "n": 1,
                           "scenes": [scene] if scene else [],
                           "rep": (obs_id, importance)}
            return
        self._event["last"] = ts
        self._event["n"] += 1
        if scene:
            self._event["scenes"].append(scene)
        if importance > self._event["rep"][1]:
            self._event["rep"] = (obs_id, importance)

    def _flush_event(self) -> None:
        ev = self._event
        self._event = None
        if not ev:
            return
        title = _dominant_scene(ev["scenes"]) or f"{ev['n']} observation(s)"
        self.store.add_event(
            start_ts=ev["start"], end_ts=ev["last"], title=title,
            n_obs=ev["n"], rep_obs_id=ev["rep"][0],
        )
        self.store.add_metric("event_built", 1, {"n_obs": ev["n"]})

    def _observation_text(self, payload: dict[str, Any]) -> str:
        """Flat text summary embedded for semantic retrieval."""
        vlm = payload.get("vlm") or {}
        parts = [vlm.get("scene", ""), vlm.get("summary", "")]
        for o in vlm.get("observations") or []:
            if isinstance(o, dict) and o.get("description"):
                parts.append(str(o["description"]))
        for a in vlm.get("actions") or []:
            if isinstance(a, dict) and a.get("description"):
                parts.append(str(a["description"]))
        if vlm.get("screen_text"):
            parts.append(str(vlm["screen_text"]))
        text = " | ".join(p for p in parts if p)
        return text[:1200]

    async def _embed_observation(self, obs_id: int, payload: dict[str, Any]) -> None:
        if self.embedder is None or self._embed_failures >= 3:
            if self._embed_failures >= 3:
                self.status.embeddings_enabled = False
            return
        text = self._observation_text(payload)
        if not text.strip():
            return
        try:
            vectors = await self.embedder.embed([text])
            from ..store.db import vec_to_bytes
            self.store.set_observation_embedding(
                obs_id, len(vectors[0]), self.embedder.model, vec_to_bytes(vectors[0])
            )
            self._embed_failures = 0
            self.status.embeddings_enabled = True
        except Exception as exc:
            # Embedding is an enhancement: never break the perception loop.
            self._embed_failures += 1
            self.store.add_metric("embed_error", 1, {"error": str(exc)[:200]})
            if self._embed_failures >= 3:
                self.status.embeddings_enabled = False

    def _enforce_media_budget(self) -> None:
        """Hard byte budget: evict oldest non-important media first.

        Important (importance>=2) images are protected while anything else
        remains; only a budget smaller than the protected set touches them.
        """
        budget = self.cfg.media_budget_bytes
        if budget <= 0:
            return
        removed: list[int] = []
        while self.store.media_bytes_total() > budget:
            rows = self.store.media_rows_for_eviction(limit=10)
            if not rows:
                break
            row = rows[0]
            path = self.store.delete_media_row(row["media_id"])
            if path:
                try:
                    (self.run_dir / path).unlink(missing_ok=True)
                except OSError:
                    pass
            removed.append(row["media_id"])
        if removed:
            self.store.add_metric("media_evicted", len(removed))

    def _periodic_media_sweep(self) -> None:
        """Retention: drop media files older than the configured window."""
        retention_min = self.cfg.media_retention_minutes
        if retention_min <= 0:
            return
        now = time.monotonic()
        if now - self._last_media_sweep_mono < 60.0:
            return
        self._last_media_sweep_mono = now
        cutoff = iso(utcnow_minus(minutes=retention_min))
        for row in self.store.old_media_rows(cutoff, limit=200):
            path = self.store.delete_media_row(row["media_id"])
            if path:
                try:
                    (self.run_dir / path).unlink(missing_ok=True)
                except OSError:
                    pass

    def _periodic_index_sweep(self) -> None:
        """HTI: index closed hours while the pipeline is idle.

        Runs only when the intake queue is empty (this method is reached via
        the 1s intake timeout), so the LLM pass never competes with frame
        perception. Fire-and-forget task: generation can take tens of seconds.
        """
        if self.indexer is None or not getattr(self.indexer, "enabled", False):
            return
        now = time.monotonic()
        if now - self._last_index_sweep_mono < 60.0:
            return
        self._last_index_sweep_mono = now
        if self._index_task is not None and not self._index_task.done():
            return
        self._index_task = asyncio.create_task(self._index_run(), name="hour-index")

    async def _index_run(self) -> None:
        try:
            await self.indexer.build_missing()
        except Exception as exc:  # indexing is an enhancement; never kill the worker
            self.store.add_metric("hour_index_error", 1, {"error": str(exc)[:300]})

    def _update_recommended_interval(self) -> None:
        vlm_ms = self.status.vlm_latency_ms_ema or 1000.0
        net_ms = self._net_latency_ema or 0.0
        target = (vlm_ms + net_ms) * self.cfg.backpressure_safety
        self.status.recommended_interval_ms = int(
            clamp(target, self.cfg.min_interval_ms, self.cfg.max_interval_ms)
        )
        self.status.fps = (
            1000.0 / self.status.recommended_interval_ms
            if self.status.recommended_interval_ms
            else 0.0
        )

    def _should_save_frame(self, importance: int) -> bool:
        mode = self.cfg.save_frames
        return mode == "all" or (mode == "important" and importance >= 2)

    def _save_media(self, frame_id: int, obs_id: int, jpeg: bytes, importance: int) -> None:
        try:
            img = Image.open(io.BytesIO(jpeg))
            max_side = self.cfg.media_max_side
            if max(img.size) > max_side:
                img.thumbnail((max_side, max_side))
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=self.cfg.media_jpeg_quality)
            data = self.store.encrypt_media(buf.getvalue())  # at-rest encryption (no-op if off)
            digest = hashlib.sha256(data).hexdigest()[:16]
            rel = f"media/obs{obs_id:06d}_i{importance}_{digest}.jpg"
            path = self.run_dir / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            self.store.set_frame_media(frame_id, rel, digest, len(data))
        except Exception as exc:  # media is optional; never kill the pipeline
            self.store.add_metric("media_error", 1, {"error": str(exc)[:200]})

    async def _maybe_heartbeat(self) -> None:
        """Track liveness so static scenes still count as 'watched, no change'."""
        now = time.monotonic()
        if (now - self._last_heartbeat_mono) >= self.cfg.heartbeat_interval_s:
            self._last_heartbeat_mono = now
            self.status.last_heartbeat_ts = iso()
