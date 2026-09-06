"""Standalone VMA TCP relay.

The relay is intentionally blind to phone frames. A desktop registers an
opaque channel using a shared registration token; a phone attaches using the
channel id plus a short-lived attach secret. The desktop still authenticates
the phone's VMA device token (or challenge-response) in the normal ``hello``
message after the tunnel is established.

v2 (online connectivity):

- **Attach secrets**: phones must present the channel's attach secret in
  their handshake; random channel-id guessers are rejected before consuming
  a slot. Issued to the desktop at registration; survives desktop blips.
- **Keepalive**: idle connections are pinged every 25s; a dead peer is
  detected within ~75s instead of lingering for the OS TCP timeout.
- **Wait-for-desktop**: a phone may connect while its desktop is offline.
  The relay parks the phone up to 300s (the phone's reconnect loop keeps
  re-trying after that).
- **Channel persistence**: when the desktop drops and re-registers, the
  channel and its attach secret survive so the phone keeps working.

For Internet use, put the server behind TLS (or provide ``--certfile`` and
``--keyfile`` directly). This is hop-by-hop transport security; the VMA
protocol's challenge-response + AES-GCM end-to-end layer
(``vma.security.auth_crypto``) is what actually protects payloads from the
relay operator.

Run locally::

    python -m vma_relay.server --host 0.0.0.0 --port 8765 --reg-token secret
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

KEEPALIVE_INTERVAL_S = 25.0
KEEPALIVE_TIMEOUT_S = 75.0
PHONE_WAIT_FOR_DESKTOP_S = 300.0
PHONE_POLL_S = 2.0
# How long a channel survives with no desktop and no waiting phone, so a
# desktop network blip does not rotate the attach secret out from the phone.
CHANNEL_TTL_S = 900.0
# How long a phone may keep using a PREVIOUS attach secret after rotation
# (relay restart / channel expiry). On attach with the previous secret the
# relay returns the current one so the phone self-heals.
ATTACH_SECRET_GRACE_S = 600.0


@dataclass
class Peer:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    role: str
    channel_id: str
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_seen: float = field(default_factory=time.monotonic)

    async def send_envelope(self, kind: bytes, payload: bytes) -> None:
        async with self.send_lock:
            await write_envelope(self.writer, kind, payload)

    async def send_event(self, event: str) -> None:
        async with self.send_lock:
            await write_event(self.writer, event)

    def close_soon(self) -> None:
        if not self.writer.is_closing():
            self.writer.close()

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
    attach_secret: str = ""
    prev_attach_secret: str = ""
    prev_attach_since: float = 0.0
    # Phone connections waiting for the desktop to come back, keyed by id.
    waiting: dict[str, Peer] = field(default_factory=dict)

    def accept_attach_secret(self, presented: str) -> bool:
        """True if `presented` is the current or (recent) previous secret."""
        if presented and self.attach_secret and secrets.compare_digest(presented, self.attach_secret):
            return True
        if (presented and self.prev_attach_secret
                and secrets.compare_digest(presented, self.prev_attach_secret)
                and (time.monotonic() - self.prev_attach_since) < ATTACH_SECRET_GRACE_S):
            return True
        return False

    def rotate_attach_secret(self, new_secret: str) -> None:
        self.prev_attach_secret = self.attach_secret
        self.prev_attach_since = time.monotonic()
        self.attach_secret = new_secret

class RelayServer:
    def __init__(self, host: str, port: int, reg_token: str = "") -> None:
        self.host = host
        self.port = port
        self.reg_token = reg_token
        self.channels: dict[str, Channel] = {}
        self._lock = asyncio.Lock()

    async def serve(self, ssl_context: ssl.SSLContext | None = None) -> None:
        server = await asyncio.start_server(self.handle_client, self.host, self.port, ssl=ssl_context)
        keepalive = asyncio.create_task(self._keepalive_loop(), name="relay-keepalive")
        sockets = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
        LOG.info("VMA relay listening on %s", sockets)
        try:
            async with server:
                await server.serve_forever()
        finally:
            keepalive.cancel()

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

    # ------------------------------------------------------------ desktops

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
                channel.desktop.close_soon()
            channel.desktop = peer
            # Attach-secret continuity: a desktop re-registering with the
            # secret it already holds is a network blip — keep it so parked
            # phones stay valid. A desktop SEEDING the secret after a relay
            # restart also keeps it (the relay lost its memory, the desktop
            # didn't). Anything else mints a fresh secret (old one still
            # accepted from phones for the grace window).
            presented = str(hello.get("attach_secret") or "")
            seed = str(hello.get("seed_attach_secret") or "")
            if not channel.attach_secret:
                if seed:
                    channel.attach_secret = seed  # relay restart: trust the desktop's persisted secret
                elif presented:
                    channel.attach_secret = presented
                else:
                    channel.rotate_attach_secret(_new_secret())
            elif presented and secrets.compare_digest(presented, channel.attach_secret):
                pass  # network blip — keep the secret
            elif seed and secrets.compare_digest(seed, channel.attach_secret):
                pass  # seeded the same secret we already have
            else:
                channel.rotate_attach_secret(_new_secret())
            attach_secret = channel.attach_secret
            waiting = list(channel.waiting.values())
            channel.waiting.clear()
        await write_handshake(writer, {
            "type": "registered",
            "channel_id": channel_id,
            "attach_secret": attach_secret,
        })
        LOG.info("desktop registered channel=%s", channel_id)
        for phone in waiting:
            try:
                await self._attach_phone_authorized(phone)
            except Exception:
                await phone.close()
        return peer

    # -------------------------------------------------------------- phones

    async def _attach_phone(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, hello: dict[str, Any]
    ) -> Peer:
        channel_id = str(hello.get("channel_id") or "")
        if not _valid_channel_id(channel_id):
            raise ValueError("invalid channel id")
        attach_secret = str(hello.get("attach_secret") or "")
        wait = bool(hello.get("wait_for_desktop"))
        peer = Peer(reader, writer, "phone", channel_id)

        async with self._lock:
            channel = self.channels.get(channel_id)
            if channel is None:
                if not wait:
                    raise ValueError("desktop channel is not online")
                channel = Channel(channel_id)
                channel.rotate_attach_secret(_new_secret())
                self.channels[channel_id] = channel
            if not channel.accept_attach_secret(attach_secret):
                raise ValueError("attach secret rejected")
            stale_secret = attach_secret != channel.attach_secret
            if stale_secret:
                # The presented secret is the previous one (relay restart or
                # rotation while offline). Hand back the CURRENT secret and
                # drop this connection — the phone stores it and reconnects.
                # (Never continue the attach: the phone would re-handshake
                # while we are already in envelope mode and desync the wire.)
                try:
                    await write_handshake(writer, {
                        "type": "attach_secret_refresh", "attach_secret": channel.attach_secret,
                    })
                except Exception:
                    pass
                raise ValueError("attach secret refreshed; reconnect with the new secret")
            if channel.desktop is None:
                if not wait:
                    raise ValueError("desktop channel is not online")
                wait_id = f"w_{secrets.token_hex(6)}"
                channel.waiting[wait_id] = peer
                LOG.info("phone waiting for desktop channel=%s", channel_id)

        if channel.desktop is None:
            # Parked: poll until the desktop registers, this socket dies, or
            # the wait window expires. _register_desktop attaches parked
            # phones itself; the poll loop catches that transition.
            deadline = time.monotonic() + PHONE_WAIT_FOR_DESKTOP_S
            while time.monotonic() < deadline:
                if peer.writer.is_closing():
                    return peer  # detached by the finally block
                ch = self.channels.get(channel_id)
                if ch is not channel:
                    return peer  # channel replaced; detach
                if peer not in ch.waiting.values():
                    return peer  # attached (or dropped) by the desktop path
                await asyncio.sleep(PHONE_POLL_S)
            raise ValueError("timed out waiting for the desktop to come back online")
        await self._attach_phone_authorized(peer)
        return peer

    async def _attach_phone_authorized(self, peer: Peer) -> Peer:
        channel = self.channels.get(peer.channel_id)
        if channel is None or channel.desktop is None:
            await peer.close()
            raise ValueError("desktop channel is not online")
        async with self._lock:
            if channel.phone is not None and channel.phone is not peer:
                channel.phone.close_soon()
            channel.phone = peer
            desktop = channel.desktop
        await write_handshake(peer.writer, {"type": "attached", "channel_id": peer.channel_id})
        await desktop.send_event("phone_attached")
        LOG.info("phone attached channel=%s", peer.channel_id)
        return peer

    # ----------------------------------------------------------- forwarding

    async def _forward_loop(self, peer: Peer) -> None:
        while True:
            kind, payload = await read_envelope(peer.reader)
            peer.last_seen = time.monotonic()
            if kind == KIND_EVENT:
                try:
                    event = json.loads(payload.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                # ping/pong are relay-internal liveness traffic; everything
                # else is relay-local bookkeeping, never forwarded.
                if event.get("event") == "ping":
                    await peer.send_event("pong")
                continue
            async with self._lock:
                channel = self.channels.get(peer.channel_id)
                target = (channel.phone if peer.role == "desktop" else channel.desktop) if channel else None
            if target is None:
                continue
            await target.send_envelope(kind, payload)

    async def _keepalive_loop(self) -> None:
        while True:
            await asyncio.sleep(KEEPALIVE_INTERVAL_S)
            now = time.monotonic()
            async with self._lock:
                peers = [p for ch in self.channels.values()
                         for p in (ch.desktop, ch.phone, *ch.waiting.values()) if p is not None]
                # Reap channels that lost their desktop and have no waiting
                # phone for longer than CHANNEL_TTL_S (grace for blips).
                stale_ids = [
                    cid for cid, ch in self.channels.items()
                    if ch.desktop is None and not ch.waiting
                    and (ch.phone is None or ch.phone.writer.is_closing())
                    and now - ch.created_at > CHANNEL_TTL_S
                    and now - max((p.last_seen for p in (ch.phone,) if p is not None),
                                  default=ch.created_at) > CHANNEL_TTL_S
                ]
                for cid in stale_ids:
                    LOG.info("reaping idle channel %s", cid)
                    self.channels.pop(cid, None)
            for peer in peers:
                if now - peer.last_seen > KEEPALIVE_TIMEOUT_S:
                    LOG.info("keepalive timeout channel=%s role=%s", peer.channel_id, peer.role)
                    peer.close_soon()
                else:
                    try:
                        await peer.send_event("ping")
                    except Exception:
                        peer.close_soon()

    async def _detach(self, peer: Peer) -> None:
        async with self._lock:
            channel = self.channels.get(peer.channel_id)
            if channel is None:
                await peer.close()
                return
            for wid, w in list(channel.waiting.items()):
                if w is peer:
                    channel.waiting.pop(wid, None)
                    await peer.close()
                    return
            if peer.role == "desktop" and channel.desktop is peer:
                channel.desktop = None
                phone = channel.phone
                channel.phone = None
                # Channel PERSISTS (attach secret stays valid) so the phone
                # reconnects to the same channel when the desktop returns.
                # The keepalive loop reaps channels with no peers after
                # CHANNEL_TTL_S. Notify the phone to park/reconnect.
                remove = False
            elif peer.role == "phone" and channel.phone is peer:
                channel.phone = None
                phone = None
                remove = channel.desktop is None and not channel.waiting
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
                await phone.send_event("phone_detached")
            except Exception:
                pass
            await phone.close()
        await peer.close()


def _new_secret() -> str:
    return secrets.token_urlsafe(24)


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
    asyncio.run(RelayServer(args.host, args.port, args.reg_token).serve(
        tls_context(args.certfile, args.keyfile)))


if __name__ == "__main__":
    main()
