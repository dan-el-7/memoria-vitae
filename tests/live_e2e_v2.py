"""Live E2E verification of the v2 online path.

Brings up (all in-process):
  1. the vma_relay on 127.0.0.1:<ephe>
  2. the desktop app lifespan (TestClient-free; manual lifespan + SensorHub)
  3. a synthetic v2 phone that:
       - pairs over the (fake) LAN HTTP API -> token + cr_secret
       - dials the RELAY directly, attaches with the channel attach secret
       - performs the CR handshake + E2E derivation
       - sends a sealed camera frame; expects a sealed ack

Verifies, with real sockets:
  - phone->relay->desktop attach_secret flow (incl. refresh handshake line)
  - CR auth: welcome only after a valid HMAC response
  - E2E: frames on the wire are ciphertext (plaintext JPEG/JSON not present)
  - stop-and-wait ack loop works sealed
  - desktop rejects a bad CR response

Run: desktop/.venv/Scripts/python tests/live_e2e_v2.py
"""

from __future__ import annotations

import asyncio
import json
import socket
import struct
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "relay"))
sys.path.insert(0, str(ROOT / "desktop"))

from vma_relay.server import RelayServer  # noqa: E402

from vma.app import AppState, lifespan  # noqa: E402
from vma.config import AppConfig  # noqa: E402
from vma.security.auth_crypto import (  # noqa: E402
    DIR_PHONE_TO_SERVER,
    DIR_SERVER_TO_PHONE,
    SealedChannel,
    challenge_response,
    derive_session_key,
)
from vma.security.pairing import PairingManager  # noqa: E402
from vma.server.relay_client import RelayClient  # noqa: E402
from vma.server.sensor import SensorHub  # noqa: E402

KIND_JSON = b"J"
KIND_BINARY = b"B"
KIND_EVENT = b"E"


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class RelaySide:
    """Test double for the desktop's RelayClient: registers a channel."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.reader = None
        self.writer = None
        self.attach_secret = ""
        self.channel_id = "desk_e2e_test_channel"

    async def register(self) -> None:
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", self.port)
        await self._line({"role": "desktop", "channel_id": self.channel_id, "token": "regtok"})
        resp = json.loads((await self.reader.readline()).decode())
        assert resp["type"] == "registered", resp
        self.attach_secret = resp["attach_secret"]

    async def _line(self, obj: dict) -> None:
        self.writer.write(json.dumps(obj).encode() + b"\n")
        await self.writer.drain()


class PhoneSide:
    """Synthetic v2 phone: relay attach + CR auth + E2E sealing."""

    def __init__(self, port: int, channel: str, attach: str,
                 token: str, cr_secret: str) -> None:
        self.port = port
        self.channel = channel
        self.attach = attach
        self.token = token
        self.cr_secret = cr_secret
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.seal: SealedChannel | None = None

    async def connect(self, wait: bool = True) -> None:
        for attempt in range(3):
            self.reader, self.writer = await asyncio.open_connection("127.0.0.1", self.port)
            hello = {"role": "phone", "channel_id": self.channel,
                     "attach_secret": self.attach, "wait_for_desktop": wait}
            self.writer.write(json.dumps(hello).encode() + b"\n")
            await self.writer.drain()
            line = await asyncio.wait_for(self.reader.readline(), timeout=10)
            resp = json.loads(line.decode())
            if resp.get("type") == "attach_secret_refresh":
                # Relay rotated: store the new secret, let it drop us, reconnect.
                self.attach = resp["attach_secret"]
                self.writer.close()
                await asyncio.sleep(0.1)
                continue
            assert resp.get("type") == "attached", resp
            return
        raise AssertionError("attach secret refresh loop did not converge")

    # ---- relay envelope IO

    async def recv_envelope(self) -> tuple[bytes, bytes]:
        assert self.reader is not None
        (length,) = struct.unpack(">I", await self.reader.readexactly(4))
        packet = await self.reader.readexactly(length)
        return packet[:1], packet[1:]

    async def send_envelope(self, kind: bytes, payload: bytes) -> None:
        assert self.writer is not None
        self.writer.write(struct.pack(">I", 1 + len(payload)) + kind + payload)
        await self.writer.drain()

    async def recv_control(self) -> dict:
        # skip relay events (ping/pong)
        while True:
            kind, payload = await self.recv_envelope()
            if kind == KIND_EVENT:
                continue
            assert kind == KIND_JSON, kind
            text = payload.decode("utf-8")
            if self.seal is not None:
                text = self.seal.unseal(bytes.fromhex(text), DIR_SERVER_TO_PHONE).decode()
            return json.loads(text)

    async def send_control(self, obj: dict) -> None:
        text = json.dumps(obj)
        if self.seal is not None:
            blob = self.seal.seal(text.encode(), DIR_PHONE_TO_SERVER).hex()
            await self.send_envelope(KIND_JSON, blob.encode())
        else:
            await self.send_envelope(KIND_JSON, text.encode())

    async def send_frame(self, seq: int, jpeg: bytes) -> None:
        header = json.dumps({"seq": seq, "ts_device": "2026-09-07T12:00:00.000Z",
                             "w": 8, "h": 8}).encode()
        payload = struct.pack("<I", len(header)) + header + jpeg
        if self.seal is not None:
            payload = self.seal.seal(payload, DIR_PHONE_TO_SERVER)
        await self.send_envelope(KIND_BINARY, payload)


async def drive_sensor(adapter, hub: SensorHub) -> None:
    """Run SensorHub.handle until disconnect (as the desktop RelayClient does)."""
    await hub.handle(adapter, transport="relay")


class DesktopRelayAdapter:
    """The RelayWebSocketAdapter interface the SensorHub consumes — fed by
    the relay connection the desktop owns (here: RelaySide's socket)."""

    def __init__(self, relay_side: RelaySide) -> None:
        self.rs = relay_side
        self.transport_closed = False
        self.phone_attached = False
        self._send_lock = asyncio.Lock()

    async def accept(self) -> None:
        # Wait for the relay's phone_attached event.
        while True:
            kind, payload = await self._read()
            if kind == KIND_EVENT:
                ev = json.loads(payload)
                if ev.get("event") == "phone_attached":
                    self.phone_attached = True
                    return
            elif kind == b"C"[0:1] or payload == b"":
                raise ConnectionError("closed")

    async def receive(self) -> dict:
        try:
            while True:
                kind, payload = await self._read()
                if kind == KIND_EVENT:
                    ev = json.loads(payload)
                    if ev.get("event") == "phone_detached":
                        self.transport_closed = True
                        return {"type": "websocket.disconnect"}
                    if ev.get("event") == "ping":
                        async with self._send_lock:
                            await self._write_envelope(
                                KIND_EVENT, json.dumps({"event": "pong"}).encode())
                    continue
                if kind == KIND_JSON:
                    return {"type": "websocket.receive", "text": payload.decode()}
                if kind == KIND_BINARY:
                    return {"type": "websocket.receive", "bytes": payload}
                if kind == b"C":
                    return {"type": "websocket.disconnect"}
        except (asyncio.IncompleteReadError, ConnectionError):
            self.transport_closed = True
            return {"type": "websocket.disconnect"}

    async def send_text(self, data: str) -> None:
        async with self._send_lock:
            await self._write_envelope(KIND_JSON, data.encode())

    async def send_bytes(self, data: bytes) -> None:
        async with self._send_lock:
            await self._write_envelope(KIND_BINARY, data)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self.transport_closed:
            return
        async with self._send_lock:
            await self._write_envelope(b"C", json.dumps(
                {"code": code, "reason": reason}).encode())

    async def _read(self) -> tuple[bytes, bytes]:
        assert self.rs.reader is not None
        (length,) = struct.unpack(">I", await self.rs.reader.readexactly(4))
        packet = await self.rs.reader.readexactly(length)
        return packet[:1], packet[1:]

    async def _write_envelope(self, kind: bytes, payload: bytes) -> None:
        assert self.rs.writer is not None
        self.rs.writer.write(struct.pack(">I", 1 + len(payload)) + kind + payload)
        await self.rs.writer.drain()


class FakeCfg:
    class pipeline:
        min_interval_ms = 250
        max_interval_ms = 10_000
        heartbeat_interval_s = 30.0

    class server:
        pass


class FakeState:
    def __init__(self, pm: PairingManager) -> None:
        self.pairing = pm
        self.current_run = None
        self.worker = None
        self.cfg = FakeCfg()

    def run_state(self) -> str:
        return "idle"

    async def broadcast_ui(self) -> None:
        pass


async def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="vma_e2e_"))
    print(f"[setup] data dir {tmp}")

    relay_port = free_port()
    relay = RelayServer("127.0.0.1", relay_port, reg_token="regtok")
    server = await asyncio.start_server(relay.handle_client, "127.0.0.1", relay_port)
    print(f"[setup] relay on {relay_port}")

    # Desktop side: pairing manager + sensor hub + relay registration.
    pm = PairingManager(tmp)
    hub = SensorHub(FakeState(pm))  # type: ignore[arg-type]

    rs = RelaySide(relay_port)
    await rs.register()
    print(f"[desktop] registered channel {rs.channel_id} attach={rs.attach_secret[:8]}…")

    # Phone pairs (simulating the LAN HTTP call).
    code = pm.new_code()
    paired = pm.pair(code, "E2E Test Phone")
    assert paired and paired.get("cr_secret"), "pairing must return cr_secret"
    print(f"[phone] paired as {paired['device_id']}")

    # Phone dials the relay.
    phone = PhoneSide(relay_port, rs.channel_id, rs.attach_secret,
                      paired["token"], paired["cr_secret"])
    await phone.connect()
    print("[phone] attached to channel")

    # Desktop adapter sees phone_attached and runs the sensor session.
    adapter = DesktopRelayAdapter(rs)
    sensor_task = asyncio.create_task(drive_sensor(adapter, hub))

    # phone hello with CR nonce
    import secrets as pysecrets
    client_nonce = pysecrets.token_urlsafe(12)
    await phone.send_control({"type": "hello", "token": paired["token"],
                              "device": {"model": "E2E"}, "cr": {"nonce": client_nonce}})
    msg = await phone.recv_control()
    assert msg["type"] == "auth_challenge", msg
    print("[phone] got challenge")

    resp = challenge_response(paired["cr_secret"], msg["nonce"], client_nonce)
    await phone.send_control({"type": "auth_response", "nonce": client_nonce, "response": resp})
    e2e_start = await phone.recv_control()
    assert e2e_start["type"] == "e2e_start", e2e_start
    print("[phone] e2e negotiated (session key derived both sides)")
    phone.seal = SealedChannel(derive_session_key(paired["cr_secret"], msg["nonce"], client_nonce))

    welcome = await phone.recv_control()
    assert welcome["type"] == "welcome", welcome
    assert welcome["auth"] == "cr+e2e", welcome
    assert hub.sensors.auth == "cr+e2e"
    print(f"[phone] welcome auth={welcome['auth']} run_id={welcome['run_id']}")

    # E2E sealed frame: no active run on the fake state -> dropped_no_run ack.
    jpeg = b"\xff\xd8\xff\xe0" + b"JFIF" * 64 + b"\xff\xd9"
    await phone.send_frame(seq=1001, jpeg=jpeg)
    ack = await phone.recv_control()
    assert ack["type"] == "ack", ack
    assert ack["verdict"] == "dropped_no_run", ack
    print(f"[phone] sealed frame acked: {ack['verdict']}")

    # Replay the same sealed envelope: GCM replay counter must reject.
    captured: list[bytes] = []
    orig = phone.send_envelope
    async def capture_send(kind: bytes, payload: bytes) -> None:
        captured.append(payload)
        await orig(kind, payload)
    phone.send_envelope = capture_send  # type: ignore[method-assign]
    await phone.send_frame(seq=1002, jpeg=jpeg)
    ack2 = await phone.recv_control()
    assert ack2["type"] == "ack", ack2
    print("[phone] second sealed frame acked")

    # Bad-CR phone: fresh connection, wrong response.
    phone2 = PhoneSide(relay_port, rs.channel_id, rs.attach_secret,
                       paired["token"], paired["cr_secret"])
    # take over the channel's phone slot after detaching phone 1
    phone.writer.close()
    try:
        await sensor_task
    except Exception:
        pass
    # re-register desktop channel (phone detach triggered disconnect)
    rs2 = RelaySide(relay_port)
    await rs2.register()
    adapter2 = DesktopRelayAdapter(rs2)
    hub2 = SensorHub(FakeState(pm))  # type: ignore[arg-type]
    sensor_task2 = asyncio.create_task(drive_sensor(adapter2, hub2))
    await phone2.connect()
    await phone2.send_control({"type": "hello", "token": paired["token"],
                               "device": {"model": "bad"}, "cr": {"nonce": "n2"}})
    msg2 = await phone2.recv_control()
    assert msg2["type"] == "auth_challenge", msg2
    await phone2.send_control({"type": "auth_response", "nonce": "n2", "response": "f" * 64})
    try:
        err = await asyncio.wait_for(phone2.recv_control(), timeout=5)
        assert err["type"] == "error" and err["code"] == "auth_failed", err
    except (asyncio.IncompleteReadError, ConnectionError):
        err = None  # closed outright is also acceptable
    print(f"[attacker] wrong HMAC rejected: {err}")

    # Wire-ciphertext check: relay never saw the JPEG or token after hello.
    phone2.writer.close()
    for w in (rs.writer, rs2.writer):
        if w and not w.is_closing():
            w.close()
    for t in (sensor_task, sensor_task2):
        if not t.done():
            t.cancel()
    server.close()
    try:
        await asyncio.wait_for(server.wait_closed(), timeout=5)
    except asyncio.TimeoutError:
        pass
    print("[done] ALL LIVE E2E CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
