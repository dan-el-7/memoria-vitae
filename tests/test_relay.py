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
        assert registered["type"] == "registered"
        assert registered["channel_id"] == "desk_test_channel"
        attach_secret = registered["attach_secret"]
        assert attach_secret

        phone_reader, phone_writer = await asyncio.open_connection("127.0.0.1", port)
        await send_handshake(phone_writer, {
            "role": "phone", "channel_id": "desk_test_channel", "attach_secret": attach_secret
        })
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


@pytest.mark.asyncio
async def test_relay_rejects_phone_with_bad_attach_secret() -> None:
    """A guessed channel id alone must not get a phone attached."""
    relay = RelayServer("127.0.0.1", 0, reg_token="secret")
    server = await asyncio.start_server(relay.handle_client, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    desktop_writer: asyncio.StreamWriter | None = None
    try:
        desktop_reader, desktop_writer = await asyncio.open_connection("127.0.0.1", port)
        await send_handshake(desktop_writer, {
            "role": "desktop", "channel_id": "desk_test_channel", "token": "secret"
        })
        await desktop_reader.readline()  # registered

        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        await send_handshake(writer, {
            "role": "phone", "channel_id": "desk_test_channel", "attach_secret": "wrong"
        })
        error = json.loads((await reader.readline()).decode("utf-8"))
        assert error["type"] == "error"
        assert "attach secret" in error["message"]
        writer.close()
        await writer.wait_closed()
    finally:
        if desktop_writer is not None and not desktop_writer.is_closing():
            desktop_writer.close()
            await desktop_writer.wait_closed()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_relay_phone_waits_for_desktop_and_attaches() -> None:
    """Offline resilience: a phone may arrive before the desktop re-registers."""
    relay = RelayServer("127.0.0.1", 0, reg_token="secret")
    server = await asyncio.start_server(relay.handle_client, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    desktop_writer: asyncio.StreamWriter | None = None
    phone_writer: asyncio.StreamWriter | None = None
    try:
        # Desktop registers once to create the channel + attach secret.
        desktop_reader, desktop_writer = await asyncio.open_connection("127.0.0.1", port)
        await send_handshake(desktop_writer, {
            "role": "desktop", "channel_id": "desk_wait_channel", "token": "secret"
        })
        registered = json.loads((await desktop_reader.readline()).decode("utf-8"))
        attach_secret = registered["attach_secret"]
        # Desktop drops (network blip).
        desktop_writer.close()
        await desktop_writer.wait_closed()
        await asyncio.sleep(0.1)

        # Phone connects while the desktop is offline and parks.
        phone_reader, phone_writer = await asyncio.open_connection("127.0.0.1", port)
        await send_handshake(phone_writer, {
            "role": "phone", "channel_id": "desk_wait_channel",
            "attach_secret": attach_secret, "wait_for_desktop": True,
        })
        await asyncio.sleep(0.3)  # parked, no handshake line yet

        # Desktop comes back, presenting the SAME attach secret (continuity).
        desktop_reader, desktop_writer = await asyncio.open_connection("127.0.0.1", port)
        await send_handshake(desktop_writer, {
            "role": "desktop", "channel_id": "desk_wait_channel", "token": "secret",
            "attach_secret": attach_secret,
        })
        re_registered = json.loads((await desktop_reader.readline()).decode("utf-8"))
        assert re_registered["attach_secret"] == attach_secret  # not rotated

        # Parked phone gets attached automatically.
        attached = json.loads((await phone_reader.readline()).decode("utf-8"))
        assert attached == {"type": "attached", "channel_id": "desk_wait_channel"}
        kind, payload = await asyncio.wait_for(read_envelope(desktop_reader), timeout=2)
        assert kind == KIND_EVENT
        assert json.loads(payload)["event"] == "phone_attached"
    finally:
        for writer in (phone_writer, desktop_writer):
            if writer is not None and not writer.is_closing():
                writer.close()
                await writer.wait_closed()
        server.close()
        await server.wait_closed()


async def _write_envelope(writer: asyncio.StreamWriter, kind: bytes, payload: bytes) -> None:
    writer.write(len(payload + kind).to_bytes(4, "big") + kind + payload)
    await writer.drain()
