"""Wire helpers shared by the standalone relay implementation.

The first packet on a connection is a newline-delimited JSON handshake. Once
the relay accepts it, traffic uses a length-prefixed envelope:

    [u32 big-endian envelope length][kind byte][payload]

Kinds are JSON control, binary sensor data, relay event, and close. The relay
does not inspect or modify phone protocol payloads.
"""

from __future__ import annotations

import asyncio
import json
import struct
from typing import Any

MAX_HANDSHAKE_BYTES = 16 * 1024
MAX_ENVELOPE_BYTES = 24 * 1024 * 1024

KIND_JSON = b"J"
KIND_BINARY = b"B"
KIND_EVENT = b"E"
KIND_CLOSE = b"C"


async def read_handshake(reader: asyncio.StreamReader) -> dict[str, Any]:
    raw = await asyncio.wait_for(reader.readline(), timeout=15)
    if not raw or len(raw) > MAX_HANDSHAKE_BYTES:
        raise ValueError("invalid or oversized handshake")
    message = json.loads(raw.decode("utf-8"))
    if not isinstance(message, dict):
        raise ValueError("handshake must be a JSON object")
    return message


async def write_handshake(writer: asyncio.StreamWriter, message: dict[str, Any]) -> None:
    writer.write(json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n")
    await writer.drain()


async def read_envelope(reader: asyncio.StreamReader) -> tuple[bytes, bytes]:
    raw_len = await reader.readexactly(4)
    (length,) = struct.unpack(">I", raw_len)
    if length < 1 or length > MAX_ENVELOPE_BYTES:
        raise ValueError("invalid or oversized relay envelope")
    packet = await reader.readexactly(length)
    return packet[:1], packet[1:]


async def write_envelope(writer: asyncio.StreamWriter, kind: bytes, payload: bytes) -> None:
    if len(kind) != 1:
        raise ValueError("relay envelope kind must be one byte")
    length = 1 + len(payload)
    if length > MAX_ENVELOPE_BYTES:
        raise ValueError("relay envelope is too large")
    writer.write(struct.pack(">I", length) + kind + payload)
    await writer.drain()


async def write_event(writer: asyncio.StreamWriter, event: str) -> None:
    await write_envelope(
        writer,
        KIND_EVENT,
        json.dumps({"type": "relay_event", "event": event}, separators=(",", ":")).encode("utf-8"),
    )


async def write_close(writer: asyncio.StreamWriter, code: int = 1000, reason: str = "") -> None:
    await write_envelope(
        writer,
        KIND_CLOSE,
        json.dumps({"code": code, "reason": reason[:240]}, separators=(",", ":")).encode("utf-8"),
    )

