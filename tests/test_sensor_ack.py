"""Regression tests for the sensor WebSocket hub.

Guards the stop-and-wait deadlock: EVERY submitted frame must receive an ack,
duplicates included. A missing ack freezes the phone permanently (it keeps at
most one frame in flight), which is how "80 frames sent, 3 stored" happened.
"""

from __future__ import annotations

import asyncio
import json
import struct
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "desktop"))

from vma.pipeline.intake import FrameIntake
from vma.pipeline.worker import FrameAck
from vma.server.sensor import SensorHub


class FakeWorker:
    """Mimics PipelineWorker.submit using a real FrameIntake (dedup included)."""

    def __init__(self) -> None:
        self.intake = FrameIntake(capacity=3)

    async def submit(self, frame) -> FrameAck:
        verdict = await self.intake.put(frame)
        return FrameAck(seq=frame.seq, verdict=verdict, recommended_interval_ms=1000, queue_depth=1)


class FakeWs:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(text)


def make_hub() -> SensorHub:
    state = types.SimpleNamespace(
        current_run=types.SimpleNamespace(id="run_x"),
        worker=FakeWorker(),
        cfg=types.SimpleNamespace(pipeline=types.SimpleNamespace(max_interval_ms=10_000)),
        pairing=types.SimpleNamespace(verify_token=lambda t: "dev_1"),
    )
    return SensorHub(state)


def frame_packet(seq: int) -> bytes:
    header = json.dumps({"seq": seq, "ts_device": "2026-08-30T10:00:00.000Z", "w": 4, "h": 4}).encode()
    return struct.pack("<I", len(header)) + header + b"\xff\xd8jpeg"


async def drive(hub: SensorHub, ws: FakeWs, packets: list[bytes]) -> None:
    hub.sensors.device_id = "dev_1"  # simulate a completed hello
    for p in packets:
        await hub._on_binary(ws, p)


@pytest.mark.asyncio
async def test_duplicate_still_acked():
    hub = make_hub()
    ws = FakeWs()
    await drive(hub, ws, [frame_packet(1), frame_packet(2), frame_packet(1)])
    acks = [json.loads(s) for s in ws.sent]
    assert [a["type"] for a in acks] == ["ack", "ack", "ack"]
    assert [a["verdict"] for a in acks] == ["accepted", "accepted", "duplicate"]


@pytest.mark.asyncio
async def test_replayed_buffer_after_reconnect_all_acked():
    """Simulates the phone replaying its offline buffer: every frame gets an
    ack, so the phone's stop-and-wait always advances."""
    hub = make_hub()
    ws = FakeWs()
    packets = [frame_packet(s) for s in (10, 11, 12)]
    await drive(hub, ws, packets + packets)  # send twice, as a reconnect replay would
    acks = [json.loads(s) for s in ws.sent]
    assert len(acks) == 6
    assert all(a["type"] == "ack" for a in acks)
    dupes = [a for a in acks if a["verdict"] == "duplicate"]
    assert len(dupes) == 3
    assert all(a["rec_interval_ms"] > 0 for a in acks)


@pytest.mark.asyncio
async def test_unauthenticated_frames_rejected_without_crash():
    state = types.SimpleNamespace(
        current_run=None, worker=FakeWorker(),
        cfg=types.SimpleNamespace(pipeline=types.SimpleNamespace(max_interval_ms=10_000)),
        pairing=types.SimpleNamespace(verify_token=lambda t: None),
    )
    hub = SensorHub(state)
    hub.sensors.device_id = "unknown"  # hello never succeeded
    ws = FakeWs()
    await hub._on_binary(ws, frame_packet(1))
    msg = json.loads(ws.sent[0])
    assert msg["type"] == "error" and msg["code"] == "not_authenticated"
