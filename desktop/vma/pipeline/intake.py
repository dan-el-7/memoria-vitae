"""Frame intake: bounded queue with latest-frame-wins backpressure.

The phone may push frames faster than the VLM can process them. The intake
holds at most `capacity` pending frames; when full, the NEWEST frame replaces
the oldest pending one (never queued indefinitely) and the drop is counted so
the recommended interval can rise. Replayed sequences (after reconnect) are
dropped as duplicates before entering the queue.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Frame:
    seq: int
    ts_device: str | None
    jpeg: bytes
    gps: dict[str, Any] | None = None
    width: int = 0
    height: int = 0
    received_ms: float = 0.0


@dataclass
class IntakeStats:
    received: int = 0
    duplicates: int = 0
    dropped_stale: int = 0
    queued_now: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "received": self.received,
            "duplicates": self.duplicates,
            "dropped_stale": self.dropped_stale,
            "queued_now": self.queued_now,
        }


class FrameIntake:
    def __init__(self, capacity: int = 3) -> None:
        self.capacity = max(1, capacity)
        self._queue: asyncio.Queue[Frame] = asyncio.Queue(maxsize=self.capacity)
        self._seen_seqs: set[int] = set()
        self._pending_seqs: set[int] = set()
        self.stats = IntakeStats()
        self._lock = asyncio.Lock()

    async def put(self, frame: Frame) -> str:
        """Enqueue a frame. Returns verdict: accepted|duplicate|stale_dropped."""
        async with self._lock:
            self.stats.received += 1
            if frame.seq in self._seen_seqs or frame.seq in self._pending_seqs:
                self.stats.duplicates += 1
                return "duplicate"
            self._pending_seqs.add(frame.seq)
            try:
                self._queue.put_nowait(frame)
                self.stats.queued_now = self._queue.qsize()
                return "accepted"
            except asyncio.QueueFull:
                # Latest wins: evict the oldest pending frame, take its seq out.
                evicted = self._queue.get_nowait()
                self._pending_seqs.discard(evicted.seq)
                self.stats.dropped_stale += 1
                self._queue.put_nowait(frame)
                self.stats.queued_now = self._queue.qsize()
                return "stale_dropped"

    async def get(self) -> Frame:
        frame = await self._queue.get()
        async with self._lock:
            self._pending_seqs.discard(frame.seq)
            self._seen_seqs.add(frame.seq)
            self.stats.queued_now = self._queue.qsize()
            # Bound dedup memory: keep the most recent 10k seqs only.
            if len(self._seen_seqs) > 10_000:
                self._seen_seqs = set(sorted(self._seen_seqs)[-5_000:])
        return frame

    def qsize(self) -> int:
        return self._queue.qsize()
