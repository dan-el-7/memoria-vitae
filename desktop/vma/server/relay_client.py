"""Outbound desktop client for the standalone VMA relay.

The relay connection is adapted to the same small interface used by
``SensorHub``. Phone control and binary frame payloads therefore stay on the
existing `/ws/phone` protocol, including token authentication and acks.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import ssl
import struct
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..utils import read_json, write_json

MAX_ENVELOPE_BYTES = 24 * 1024 * 1024
KIND_JSON = b"J"
KIND_BINARY = b"B"
KIND_EVENT = b"E"
KIND_CLOSE = b"C"


class RelayClient:
    def __init__(self, state: Any) -> None:
        self.state = state
        self.channel_id = self._load_channel_id()

    async def run(self) -> None:
        attempt = 0
        while True:
            writer: asyncio.StreamWriter | None = None
            try:
                self._set_status("connecting", connected=False)
                host, port, tls = _endpoint(self.state.cfg.server.relay_url)
                context = ssl.create_default_context() if tls else None
                reader, writer = await asyncio.open_connection(
                    host, port, ssl=context, server_hostname=host if tls else None
                )
                await _write_line(writer, {
                    "role": "desktop",
                    "channel_id": self.channel_id,
                    "token": self.state.cfg.server.relay_reg_token,
                })
                response = await asyncio.wait_for(reader.readline(), timeout=15)
                hello = json.loads(response.decode("utf-8"))
                if hello.get("type") != "registered":
                    raise RuntimeError(hello.get("message", "relay registration failed"))
                attempt = 0
                self._set_status("registered", connected=True)
                adapter = RelayWebSocketAdapter(reader, writer)
                sensor = self.state.sensor
                if sensor is None:
                    raise RuntimeError("sensor hub is not initialized")
                while not adapter.transport_closed:
                    await sensor.handle(adapter, transport="relay")
                raise ConnectionError("relay connection closed")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._set_status(f"offline: {exc}", connected=False)
                delay = min(30.0, 2 ** min(attempt, 5))
                attempt += 1
                await asyncio.sleep(delay + secrets.randbelow(300) / 1000.0)
            finally:
                if writer is not None:
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except Exception:
                        pass

    def _load_channel_id(self) -> str:
        path = Path(self.state.cfg.server.data_dir) / "relay_channel.json"
        try:
            value = str(read_json(path).get("channel_id") or "")
            if _valid_channel_id(value):
                self.state.relay_channel_id = value
                return value
        except (OSError, ValueError, AttributeError):
            pass
        value = f"desk_{secrets.token_hex(12)}"
        write_json(path, {"channel_id": value})
        self.state.relay_channel_id = value
        return value

    def _set_status(self, status: str, connected: bool) -> None:
        self.state.relay_status = status
        self.state.relay_connected = connected


class RelayWebSocketAdapter:
    """Minimal WebSocket-shaped adapter consumed by ``SensorHub``."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self._send_lock = asyncio.Lock()
        self.transport_closed = False
        self.phone_attached = False

    async def accept(self) -> None:
        """Wait until the relay has a phone before exposing a WS session."""
        if self.phone_attached:
            return
        while True:
            try:
                kind, payload = await _read_envelope(self.reader)
            except (asyncio.IncompleteReadError, ConnectionError, ValueError):
                self.transport_closed = True
                raise ConnectionError("relay connection closed while waiting for phone")
            if kind == KIND_EVENT:
                try:
                    event = json.loads(payload.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if event.get("event") == "phone_attached":
                    self.phone_attached = True
                    return
                continue
            if kind == KIND_CLOSE:
                self.transport_closed = True
                raise ConnectionError("relay closed the desktop channel")

    async def receive(self) -> dict[str, Any]:
        while True:
            try:
                kind, payload = await _read_envelope(self.reader)
            except (asyncio.IncompleteReadError, ConnectionError, ValueError):
                self.transport_closed = True
                return {"type": "websocket.disconnect"}
            if kind == KIND_EVENT:
                try:
                    event = json.loads(payload.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if event.get("event") == "phone_detached":
                    self.phone_attached = False
                    # Re-register the desktop channel so a later phone can
                    # attach cleanly without leaving SensorHub marked online.
                    self.transport_closed = True
                    return {"type": "websocket.disconnect"}
                continue
            if kind == KIND_JSON:
                return {"type": "websocket.receive", "text": payload.decode("utf-8")}
            if kind == KIND_BINARY:
                return {"type": "websocket.receive", "bytes": payload}
            if kind == KIND_CLOSE:
                return {"type": "websocket.disconnect"}

    async def send_text(self, data: str) -> None:
        async with self._send_lock:
            await _write_envelope(self.writer, KIND_JSON, data.encode("utf-8"))

    async def send_bytes(self, data: bytes) -> None:
        async with self._send_lock:
            await _write_envelope(self.writer, KIND_BINARY, data)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self.transport_closed:
            return
        async with self._send_lock:
            try:
                payload = json.dumps({"code": code, "reason": reason[:240]},
                                     separators=(",", ":")).encode("utf-8")
                await _write_envelope(self.writer, KIND_CLOSE, payload)
            except Exception:
                self.transport_closed = True


async def _write_line(writer: asyncio.StreamWriter, message: dict[str, Any]) -> None:
    writer.write(json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n")
    await writer.drain()


async def _read_envelope(reader: asyncio.StreamReader) -> tuple[bytes, bytes]:
    raw_len = await reader.readexactly(4)
    (length,) = struct.unpack(">I", raw_len)
    if length < 1 or length > MAX_ENVELOPE_BYTES:
        raise ValueError("invalid relay envelope")
    packet = await reader.readexactly(length)
    return packet[:1], packet[1:]


async def _write_envelope(writer: asyncio.StreamWriter, kind: bytes, payload: bytes) -> None:
    length = 1 + len(payload)
    if len(kind) != 1 or length > MAX_ENVELOPE_BYTES:
        raise ValueError("invalid relay envelope")
    writer.write(struct.pack(">I", length) + kind + payload)
    await writer.drain()


def _endpoint(raw: str) -> tuple[str, int, bool]:
    value = raw.strip()
    parsed = urlparse(value if "://" in value else f"tcp://{value}")
    host = parsed.hostname
    if not host:
        raise ValueError("relay_url must include a host")
    tls = parsed.scheme in {"tls", "ssl", "https", "wss"}
    return host, parsed.port or 8765, tls


def _valid_channel_id(value: str) -> bool:
    return 8 <= len(value) <= 100 and all(c.isalnum() or c in "_-" for c in value)
