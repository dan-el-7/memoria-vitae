"""Standalone VMA TCP relay.

The relay is intentionally blind to phone frames. A desktop registers an
opaque channel using a shared registration token; a phone attaches using the
channel id. The desktop still authenticates the phone's VMA device token in
the normal ``hello`` message after the tunnel is established.

Run locally::

    python -m vma_relay.server --host 0.0.0.0 --port 8765 --reg-token secret

For Internet use, put the server behind TLS (or provide ``--certfile`` and
``--keyfile`` directly). This is hop-by-hop transport security; end-to-end
payload encryption remains a future enhancement.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import secrets
import ssl
import time
from dataclasses import dataclass, field
from typing import Any

from .protocol import (
    KIND_BINARY,
    KIND_CLOSE,
    KIND_EVENT,
    KIND_JSON,
    read_envelope,
    read_handshake,
    write_envelope,
    write_event,
    write_handshake,
)

LOG = logging.getLogger("vma_relay")


@dataclass
class Peer:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    role: str
    channel_id: str
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send_envelope(self, kind: bytes, payload: bytes) -> None:
        async with self.send_lock:
            await write_envelope(self.writer, kind, payload)

    async def send_event(self, event: str) -> None:
        async with self.send_lock:
            await write_event(self.writer, event)

    async def close(self) -> None:
        if not self.writer.is_closing():
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except Exception:
                pass


@dataclass
class Channel:
    channel_id: str
    created_at: float = field(default_factory=time.time)
    desktop: Peer | None = None
    phone: Peer | None = None


class RelayServer:
    def __init__(self, host: str, port: int, reg_token: str = "") -> None:
        self.host = host
        self.port = port
        self.reg_token = reg_token
        self.channels: dict[str, Channel] = {}
        self._lock = asyncio.Lock()

    async def serve(self, ssl_context: ssl.SSLContext | None = None) -> None:
        server = await asyncio.start_server(self.handle_client, self.host, self.port, ssl=ssl_context)
        sockets = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
        LOG.info("VMA relay listening on %s", sockets)
        async with server:
            await server.serve_forever()

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer: Peer | None = None
        try:
            hello = await read_handshake(reader)
            role = str(hello.get("role") or "")
            if role == "desktop":
                peer = await self._register_desktop(reader, writer, hello)
            elif role == "phone":
                peer = await self._attach_phone(reader, writer, hello)
            else:
                await write_handshake(writer, {"type": "error", "message": "role must be desktop or phone"})
                return
            await self._forward_loop(peer)
        except (asyncio.IncompleteReadError, asyncio.TimeoutError):
            pass
        except (ValueError, json.JSONDecodeError) as exc:
            LOG.info("rejecting relay connection: %s", exc)
            try:
                await write_handshake(writer, {"type": "error", "message": str(exc)})
            except Exception:
                pass
        except Exception:
            LOG.exception("relay connection failed")
        finally:
            if peer is not None:
                await self._detach(peer)
            elif not writer.is_closing():
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass

    async def _register_desktop(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, hello: dict[str, Any]
    ) -> Peer:
        if self.reg_token and not secrets.compare_digest(str(hello.get("token") or ""), self.reg_token):
            raise ValueError("desktop registration token rejected")
        channel_id = str(hello.get("channel_id") or "")
        if not _valid_channel_id(channel_id):
            raise ValueError("invalid channel id")
        peer = Peer(reader, writer, "desktop", channel_id)
        async with self._lock:
            channel = self.channels.setdefault(channel_id, Channel(channel_id))
            if channel.desktop is not None:
                await channel.desktop.close()
            channel.desktop = peer
        await write_handshake(writer, {"type": "registered", "channel_id": channel_id})
        LOG.info("desktop registered channel=%s", channel_id)
        return peer

    async def _attach_phone(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, hello: dict[str, Any]
    ) -> Peer:
        channel_id = str(hello.get("channel_id") or "")
        if not _valid_channel_id(channel_id):
            raise ValueError("invalid channel id")
        async with self._lock:
            channel = self.channels.get(channel_id)
            if channel is None or channel.desktop is None:
                raise ValueError("desktop channel is not online")
            peer = Peer(reader, writer, "phone", channel_id)
            if channel.phone is not None:
                await channel.phone.close()
            channel.phone = peer
            desktop = channel.desktop
        await write_handshake(writer, {"type": "attached", "channel_id": channel_id})
        await desktop.send_event("phone_attached")
        LOG.info("phone attached channel=%s", channel_id)
        return peer

    async def _forward_loop(self, peer: Peer) -> None:
        while True:
            kind, payload = await read_envelope(peer.reader)
            async with self._lock:
                channel = self.channels.get(peer.channel_id)
                target = (channel.phone if peer.role == "desktop" else channel.desktop) if channel else None
            if target is None:
                continue
            await target.send_envelope(kind, payload)

    async def _detach(self, peer: Peer) -> None:
        async with self._lock:
            channel = self.channels.get(peer.channel_id)
            if channel is None:
                await peer.close()
                return
            if peer.role == "desktop" and channel.desktop is peer:
                channel.desktop = None
                phone = channel.phone
                channel.phone = None
                remove = True
            elif peer.role == "phone" and channel.phone is peer:
                channel.phone = None
                phone = None
                remove = channel.desktop is None
            else:
                phone = None
                remove = False
            if remove:
                self.channels.pop(peer.channel_id, None)
            desktop = channel.desktop
        if desktop is not None and peer.role == "phone":
            try:
                await desktop.send_event("phone_detached")
            except Exception:
                pass
        if phone is not None:
            try:
                await phone.send_event("desktop_detached")
            except Exception:
                pass
            await phone.close()
        await peer.close()


def _valid_channel_id(value: str) -> bool:
    return 8 <= len(value) <= 100 and all(c.isalnum() or c in "_-" for c in value)


def tls_context(certfile: str | None, keyfile: str | None) -> ssl.SSLContext | None:
    if not certfile and not keyfile:
        return None
    if not certfile or not keyfile:
        raise SystemExit("--certfile and --keyfile must be provided together")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile, keyfile)
    return context


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--reg-token", default="", help="shared desktop registration token")
    parser.add_argument("--certfile")
    parser.add_argument("--keyfile")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(RelayServer(args.host, args.port, args.reg_token).serve(tls_context(args.certfile, args.keyfile)))


if __name__ == "__main__":
    main()

