"""Integration checks for the standalone relay's registration and forwarding."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "relay"))

from vma_relay.protocol import KIND_BINARY, KIND_EVENT, KIND_JSON, read_envelope
from vma_relay.server import RelayServer


async def send_handshake(writer: asyncio.StreamWriter, message: dict[str, str]) -> None:
    writer.write(json.dumps(message).encode("utf-8") + b"\n")
    await writer.drain()


@pytest.mark.asyncio
async def test_relay_register_attach_and_forward() -> None:
    relay = RelayServer("127.0.0.1", 0, reg_token="secret")
    server = await asyncio.start_server(relay.handle_client, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    desktop_writer: asyncio.StreamWriter | None = None
    phone_writer: asyncio.StreamWriter | None = None
    try:
        desktop_reader, desktop_writer = await asyncio.open_connection("127.0.0.1", port)
        await send_handshake(desktop_writer, {
            "role": "desktop", "channel_id": "desk_test_channel", "token": "secret"
        })
        registered = json.loads((await desktop_reader.readline()).decode("utf-8"))
        assert registered == {"type": "registered", "channel_id": "desk_test_channel"}

        phone_reader, phone_writer = await asyncio.open_connection("127.0.0.1", port)
        await send_handshake(phone_writer, {"role": "phone", "channel_id": "desk_test_channel"})
        attached = json.loads((await phone_reader.readline()).decode("utf-8"))
        assert attached == {"type": "attached", "channel_id": "desk_test_channel"}

        kind, payload = await asyncio.wait_for(read_envelope(desktop_reader), timeout=2)
        assert kind == KIND_EVENT
        assert json.loads(payload) == {"type": "relay_event", "event": "phone_attached"}

        hello = b'{"type":"hello","token":"device-token"}'
        await _write_envelope(phone_writer, KIND_JSON, hello)
        kind, payload = await asyncio.wait_for(read_envelope(desktop_reader), timeout=2)
        assert (kind, payload) == (KIND_JSON, hello)

        frame = b"\x00\x01jpeg-frame"
        await _write_envelope(desktop_writer, KIND_BINARY, frame)
        kind, payload = await asyncio.wait_for(read_envelope(phone_reader), timeout=2)
        assert (kind, payload) == (KIND_BINARY, frame)

        phone_writer.close()
        await phone_writer.wait_closed()
        kind, payload = await asyncio.wait_for(read_envelope(desktop_reader), timeout=2)
        assert kind == KIND_EVENT
        assert json.loads(payload)["event"] == "phone_detached"
    finally:
        for writer in (phone_writer, desktop_writer):
            if writer is not None and not writer.is_closing():
                writer.close()
                await writer.wait_closed()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_relay_rejects_bad_registration_token() -> None:
    relay = RelayServer("127.0.0.1", 0, reg_token="secret")
    server = await asyncio.start_server(relay.handle_client, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        await send_handshake(writer, {
            "role": "desktop", "channel_id": "desk_test_channel", "token": "wrong"
        })
        error = json.loads((await reader.readline()).decode("utf-8"))
        assert error["type"] == "error"
        assert "registration token" in error["message"]
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


async def _write_envelope(writer: asyncio.StreamWriter, kind: bytes, payload: bytes) -> None:
    writer.write(len(payload + kind).to_bytes(4, "big") + kind + payload)
    await writer.drain()

