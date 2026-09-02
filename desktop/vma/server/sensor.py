"""Phone sensor transport: WebSocket protocol between phone and desktop.

Framing
-------
Control messages: JSON text frames.
Frame uploads: binary messages = uint32-LE header length + JSON header + JPEG.

Header: {"seq": 123, "ts_device": "...", "gps": {"lat":..., "lon":...,
"accuracy_m":..., "speed_mps":..., "ts": "..."}, "w": 1080, "h": 810}

Server -> phone:
- {"type":"welcome", "run_id":..., "min_interval_ms":..., "heartbeat_s":...}
- {"type":"ack", "seq":..., "verdict":"accepted|nochange|stale_dropped|duplicate",
   "rec_interval_ms":..., "queue":...}                 (one per submitted frame)
- {"type":"status", "run_state":"running|degraded|paused|stopped", "models":...}
- {"type":"error", "code":..., "message":...}

Backpressure: every ack carries `rec_interval_ms` (from the worker's EMA of
VLM latency + network EMA). The phone adapts its capture timer; it also keeps
at most one unacked frame in flight.
"""

from __future__ import annotations

import asyncio
import json
import struct
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from fastapi import WebSocket, WebSocketDisconnect

from ..pipeline.intake import Frame
from ..utils import iso

if TYPE_CHECKING:  # pragma: no cover
    from ..app import AppState

MAGIC_HEADER_LEN = 4  # uint32 LE


def hello_authenticated(hub: "SensorHub") -> bool:
    """True once a hello with a valid device token has been processed."""
    return hub.sensors.device_id not in ("", "unknown")


@dataclass
class SensorState:
    connected: bool = False
    device_id: str = "unknown"
    device_info: dict[str, Any] = field(default_factory=dict)
    last_seq: int = 0
    last_frame_ts: str | None = None
    connected_since: str | None = None
    last_disconnect_ts: str | None = None
    transport: str = "lan"  # lan|relay

    def to_dict(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "device_id": self.device_id,
            "device_info": self.device_info,
            "last_seq": self.last_seq,
            "last_frame_ts": self.last_frame_ts,
            "connected_since": self.connected_since,
            "last_disconnect_ts": self.last_disconnect_ts,
            "transport": self.transport,
        }


class SensorHub:
    """One active phone connection at a time; runs survive disconnects."""

    def __init__(self, state: "AppState") -> None:
        self.state = state
        self.sensors: SensorState = SensorState()
        self._ws: WebSocket | None = None
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self.sensors.connected

    async def handle(self, ws: WebSocket, transport: str = "lan") -> None:
        await ws.accept()
        async with self._lock:
            if self._ws is not None:
                await ws.close(code=4000, reason="another device is connected")
                return
            self._ws = ws
            self.sensors = SensorState(connected=True, transport=transport,
                                       connected_since=iso())
        hello_seen = False
        try:
            while True:
                message = await ws.receive()
                if message.get("type") == "websocket.disconnect":
                    break
                if (data := message.get("bytes")) is not None:
                    await self._on_binary(ws, data)
                elif (text := message.get("text")) is not None:
                    hello_seen = await self._on_control(ws, text, hello_seen)
        except WebSocketDisconnect:
            pass
        finally:
            async with self._lock:
                if self._ws is ws:
                    self._ws = None
                    self.sensors.connected = False
                    self.sensors.last_disconnect_ts = iso()
                    run = self.state.current_run
                    if run:
                        run.store.add_device_event("disconnect", {"device_id": self.sensors.device_id})
                    await self.state.broadcast_ui()

    # ------------------------------------------------------------ handlers

    async def _on_control(self, ws: WebSocket, text: str, hello_seen: bool) -> bool:
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            await self._send(ws, {"type": "error", "code": "bad_json", "message": "control message was not JSON"})
            return hello_seen
        mtype = msg.get("type")

        if mtype == "hello":
            token = str(msg.get("token") or "")
            device_id = self.state.pairing.verify_token(token)
            if device_id is None:
                await self._send(ws, {"type": "error", "code": "auth_failed",
                                      "message": "unknown or revoked device token; pair first"})
                await ws.close(code=4001, reason="authentication failed")
                return hello_seen
            device = msg.get("device") or {}
            self.sensors.device_id = device_id
            self.sensors.device_info = device
            run = self.state.current_run
            await self._send(ws, {
                "type": "welcome",
                "run_id": run.id if run else None,
                "run_state": self.state.run_state(),
                "min_interval_ms": self.state.cfg.pipeline.min_interval_ms,
                "heartbeat_s": self.state.cfg.pipeline.heartbeat_interval_s,
            })
            if run:
                run.store.add_device_event("connect", {"device_id": self.sensors.device_id, "device": device})
            await self.state.broadcast_ui()
            return True

        if mtype == "heartbeat":
            run = self.state.current_run
            gps = msg.get("gps")
            if run and gps and gps.get("lat") is not None:
                run.store.add_location(
                    msg.get("ts") or iso(), gps["lat"], gps["lon"],
                    gps.get("accuracy_m"), gps.get("speed_mps"), source="heartbeat",
                )
            self.sensors.last_frame_ts = iso()
            return hello_seen

        if mtype == "ping":
            await self._send(ws, {"type": "pong", "ts": iso()})
            return hello_seen

        if mtype == "command":
            # Mobile remote command over the already-authenticated sensor
            # socket (same device token as `hello`). Allowlist lives in
            # app.execute_command; no shell, no arbitrary execution.
            if not hello_authenticated(self):
                await self._send(ws, {"type": "error", "code": "not_authenticated",
                                      "message": "send hello with a valid device token first"})
                return hello_seen
            from ..app import execute_command
            try:
                result = await execute_command(
                    self.state, str(msg.get("command") or ""), msg.get("args") or {},
                )
                await self._send(ws, {"type": "command_result", "ok": True,
                                      "command": msg.get("command"), "result": result})
            except ValueError as exc:
                await self._send(ws, {"type": "command_result", "ok": False,
                                      "command": msg.get("command"), "error": str(exc)})
            except Exception as exc:
                await self._send(ws, {"type": "command_result", "ok": False,
                                      "command": msg.get("command"),
                                      "error": f"{type(exc).__name__}: {exc}"})
            return hello_seen

        await self._send(ws, {"type": "error", "code": "unknown_type", "message": f"unknown control type {mtype!r}"})
        return hello_seen

    async def _on_binary(self, ws: WebSocket, data: bytes) -> None:
        if not hello_authenticated(self):
            await self._send(ws, {"type": "error", "code": "not_authenticated",
                                  "message": "send hello with a valid device token first"})
            return
        if len(data) < MAGIC_HEADER_LEN:
            return
        (header_len,) = struct.unpack_from("<I", data, 0)
        if header_len > 64 * 1024 or MAGIC_HEADER_LEN + header_len > len(data):
            await self._send(ws, {"type": "error", "code": "bad_frame", "message": "invalid frame header"})
            return
        try:
            header = json.loads(data[MAGIC_HEADER_LEN:MAGIC_HEADER_LEN + header_len])
        except json.JSONDecodeError:
            await self._send(ws, {"type": "error", "code": "bad_frame", "message": "header not JSON"})
            return
        jpeg = data[MAGIC_HEADER_LEN + header_len:]
        if not jpeg:
            return

        run = self.state.current_run
        worker = self.state.worker
        if run is None or worker is None:
            await self._send(ws, {
                "type": "ack", "seq": header.get("seq", 0), "verdict": "dropped_no_run",
                "rec_interval_ms": self.state.cfg.pipeline.max_interval_ms, "queue": 0,
            })
            return

        # Cheap RTT estimate from device timestamp, if clocks are roughly sync'd.
        t0 = time.monotonic()
        frame = Frame(
            seq=int(header.get("seq", 0)),
            ts_device=header.get("ts_device"),
            jpeg=jpeg,
            gps=header.get("gps"),
            width=int(header.get("w") or 0),
            height=int(header.get("h") or 0),
        )
        ack = await worker.submit(frame)
        ack.detail += f" net_rtt_ms={((time.monotonic() - t0) * 1000):.1f}"
        self.sensors.last_seq = frame.seq
        self.sensors.last_frame_ts = iso()
        # ALWAYS ack, duplicates included: the phone is stop-and-wait, so a
        # missing ack (e.g. replayed seq after reconnect) would deadlock it.
        await self._send(ws, {
            "type": "ack",
            "seq": ack.seq,
            "verdict": ack.verdict,
            "rec_interval_ms": ack.recommended_interval_ms,
            "queue": ack.queue_depth,
        })

    async def _send(self, ws: WebSocket, obj: dict[str, Any]) -> None:
        try:
            await ws.send_text(json.dumps(obj, ensure_ascii=False))
        except Exception:
            pass

    async def push_status(self) -> None:
        if self._ws is not None:
            await self._send(self._ws, {"type": "status", "run_state": self.state.run_state()})
