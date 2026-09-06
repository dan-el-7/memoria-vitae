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

Authentication (v2)
-------------------
After pairing, the phone holds (device_token, cr_secret). Connection flow:

1. phone -> {"type":"hello", "token":..., "device":{...}, "cr": {"nonce": <client_nonce>}}
   (``cr.nonce`` present = "I do challenge-response".)
2. server verifies the token; if the device has a CR secret it replies
   {"type":"auth_challenge","nonce": <server_nonce>}  and defers welcome.
   A legacy phone (no cr field) gets the bearer-token welcome directly.
3. phone -> {"type":"auth_response","nonce": <client_nonce>,
             "response": HMAC-SHA256(cr_secret, "vma-auth-v1:<server_nonce>:<client_nonce>")}
4. server verifies; both sides derive the session key
   HKDF-SHA256(cr_secret, salt=<server_nonce>+<client_nonce>) and the server
   sends {"type":"e2e_start"} — after which ALL control text frames are
   hex-encoded AES-256-GCM sealed blobs and binary frame uploads are sealed
   too. Unsealed traffic after e2e_start is rejected (downgrade protection).

The long-lived secret never crosses the wire after pairing; a hostile relay
sees only nonces, HMACs and ciphertext. Legacy devices (paired before v2)
keep working with bearer-token hello — the dashboard shows the weaker auth
level; they should re-pair on the LAN to upgrade.

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
from ..security.auth_crypto import (
    DIR_PHONE_TO_SERVER,
    DIR_SERVER_TO_PHONE,
    SealError,
    SealedChannel,
    derive_session_key,
)
from ..utils import iso, random_token

if TYPE_CHECKING:  # pragma: no cover
    from ..app import AppState

MAGIC_HEADER_LEN = 4  # uint32 LE
AUTH_WINDOW_S = 30.0  # must complete hello(+challenge) within this window


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
    auth: str = "none"      # none|bearer|cr|cr+e2e

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
            "auth": self.auth,
        }


class SensorHub:
    """One active phone connection at a time; runs survive disconnects."""

    def __init__(self, state: "AppState") -> None:
        self.state = state
        self.sensors: SensorState = SensorState()
        self._ws: WebSocket | None = None
        self._lock = asyncio.Lock()
        # Challenge-response state for the CURRENT connection.
        self._cr_device_id: str | None = None
        self._cr_server_nonce: str | None = None
        self._cr_client_nonce: str | None = None
        self._seal: SealedChannel | None = None
        self._auth_deadline: float = 0.0

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
            self._seal = None
            self._cr_device_id = None
            self._cr_server_nonce = None
            self._cr_client_nonce = None
            self._auth_deadline = time.monotonic() + AUTH_WINDOW_S
        hello_seen = False
        try:
            while True:
                if not hello_seen and time.monotonic() > self._auth_deadline:
                    await ws.close(code=4002, reason="authentication timeout")
                    break
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
        # Unseal if the E2E layer is active.
        if self._seal is not None:
            try:
                raw = self._seal.unseal(bytes.fromhex(text), DIR_PHONE_TO_SERVER)
                text = raw.decode("utf-8")
            except (ValueError, SealError) as exc:
                await self._send(ws, {"type": "error", "code": "e2e_bad",
                                      "message": f"sealed control frame rejected: {exc}"})
                await ws.close(code=4003, reason="e2e integrity failure")
                return False
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
            self.sensors.auth = "bearer"
            cr = msg.get("cr") or {}
            client_nonce = str(cr.get("nonce") or "")
            has_secret = self.state.pairing.cr_secret_for(device_id) is not None
            if client_nonce and has_secret:
                # v2 phone: challenge it, defer the welcome.
                server_nonce = random_token(16)
                self._cr_device_id = device_id
                self._cr_server_nonce = server_nonce
                self._cr_client_nonce = client_nonce
                await self._send(ws, {"type": "auth_challenge", "nonce": server_nonce})
                return hello_seen
            if client_nonce and not has_secret:
                # Phone supports CR but this desktop has no secret stored
                # (paired pre-v2). Fall back to bearer for this connection;
                # the phone should re-pair to upgrade.
                pass
            self._cr_device_id = None
            self._cr_server_nonce = None
            await self._welcome(ws, device)
            return True

        if mtype == "auth_response":
            if self._cr_device_id is None or self._cr_server_nonce is None:
                await self._send(ws, {"type": "error", "code": "no_challenge",
                                      "message": "no active challenge; send hello first"})
                return hello_seen
            client_nonce = str(msg.get("nonce") or "")
            response = str(msg.get("response") or "")
            if client_nonce != self._cr_client_nonce:
                await self._send(ws, {"type": "error", "code": "auth_failed",
                                      "message": "challenge nonce mismatch"})
                await ws.close(code=4001, reason="authentication failed")
                return hello_seen
            if self.state.pairing.verify_challenge(self._cr_device_id, self._cr_server_nonce,
                                                   client_nonce, response):
                self.sensors.auth = "cr"
                # E2E: derive the session key from the CR secret + both nonces.
                secret = self.state.pairing.cr_secret_for(self._cr_device_id)
                if secret is not None:
                    session_key = derive_session_key(secret, self._cr_server_nonce, client_nonce)
                    channel = SealedChannel(session_key)
                    # Plaintext sentinel: everything AFTER this frame is sealed.
                    # Must be sent before self._seal is armed so it stays clear.
                    try:
                        await ws.send_text(json.dumps({"type": "e2e_start"}))
                    except Exception:
                        pass
                    self._seal = channel
                    self.sensors.auth = "cr+e2e"
                await self._welcome(ws, self.sensors.device_info)
                self._cr_device_id = None
                self._cr_server_nonce = None
                return True
            self.sensors.auth = "none"
            await self._send(ws, {"type": "error", "code": "auth_failed",
                                  "message": "challenge-response verification failed"})
            await ws.close(code=4001, reason="authentication failed")
            return hello_seen

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

    async def _welcome(self, ws: WebSocket, device: dict[str, Any]) -> None:
        run = self.state.current_run
        await self._send(ws, {
            "type": "welcome",
            "run_id": run.id if run else None,
            "run_state": self.state.run_state(),
            "min_interval_ms": self.state.cfg.pipeline.min_interval_ms,
            "heartbeat_s": self.state.cfg.pipeline.heartbeat_interval_s,
            "auth": self.sensors.auth,
        })
        if run:
            run.store.add_device_event("connect", {"device_id": self.sensors.device_id, "device": device})
        await self.state.broadcast_ui()

    async def _on_binary(self, ws: WebSocket, data: bytes) -> None:
        if not hello_authenticated(self):
            await self._send(ws, {"type": "error", "code": "not_authenticated",
                                  "message": "send hello with a valid device token first"})
            return
        if self._seal is not None:
            try:
                data = self._seal.unseal(data, DIR_PHONE_TO_SERVER)
            except SealError as exc:
                await self._send(ws, {"type": "error", "code": "e2e_bad",
                                      "message": f"sealed frame rejected: {exc}"})
                await ws.close(code=4003, reason="e2e integrity failure")
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
            text = json.dumps(obj, ensure_ascii=False)
            if self._seal is not None:
                # Sealed control frames go out as hex text (WS text frames).
                blob = self._seal.seal(text.encode("utf-8"), DIR_SERVER_TO_PHONE)
                await ws.send_text(blob.hex())
            else:
                await ws.send_text(text)
        except Exception:
            pass

    async def push_status(self) -> None:
        if self._ws is not None:
            await self._send(self._ws, {"type": "status", "run_state": self.state.run_state()})
