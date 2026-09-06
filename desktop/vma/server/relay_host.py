"""Host the VMA relay in-process when the relay URL points at this machine.

Why: users pick "this machine" as the relay in the setup wizard, then forget
to start run_relay.bat — the desktop then dials its own LAN address, gets
connection-refused, and online mode shows "offline". Hosting the relay
inside the desktop process removes the moving part: the relay listens as
part of the app, and the desktop's relay client dials 127.0.0.1.

Detection: a relay URL whose host resolves to a local address (loopback, or
one of this machine's own LAN IPs) counts as "here". External hosts (VPS)
still use the external relay — nothing changes for them.

The hosted relay runs on the port from the relay URL (default 8765) with the
configured registration token. An already-running external relay on that
port is respected: if the port is taken, hosting is skipped and the client
dials out normally (which will reach that other relay).
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

LOG = logging.getLogger("vma.relay_host")

RELAY_MODULE_HINT = "relay"  # repo layout: <root>/relay/vma_relay


def _iter_local_ips() -> set[str]:
    ips = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, family=socket.AF_INET)
        ips |= {i[4][0] for i in infos}
    except OSError:
        pass
    return ips


def parse_relay_endpoint(raw: str) -> tuple[str, int]:
    """(host, port) from tcp://host:port / tls://host:port / host:port."""
    value = (raw or "").strip()
    parsed = urlparse(value if "://" in value else f"tcp://{value}")
    host = parsed.hostname or ""
    port = parsed.port or 8765
    return host, port


def relay_is_local(raw: str) -> bool:
    """True when the relay URL points at this machine (loopback or own LAN IP)."""
    host, _ = parse_relay_endpoint(raw)
    if not host:
        return False
    if host in ("localhost", "::1") or host in _iter_local_ips():
        return True
    try:
        addr = ipaddress.ip_address(host)
        if addr.is_loopback:
            return True
    except ValueError:
        pass
    # Resolve and compare against local interfaces.
    try:
        wanted = {i[4][0] for i in socket.getaddrinfo(host, None, family=socket.AF_INET)}
        return bool(wanted & _iter_local_ips())
    except OSError:
        return False


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


class HostedRelay:
    """Runs vma_relay.server inside this process (desktop app lifespan)."""

    def __init__(self, host: str, port: int, reg_token: str) -> None:
        self.host = host
        self.port = port
        self.reg_token = reg_token
        self._server: asyncio.AbstractServer | None = None
        self._relay: Any = None

    async def start(self) -> bool:
        """Start listening. False (and log why) if the port is taken or the
        relay package cannot be imported."""
        if port_in_use(self.port):
            LOG.info("port %s already in use — assuming an external relay runs there; "
                     "not hosting in-process", self.port)
            return False
        try:
            root = Path(__file__).resolve().parent.parent.parent  # desktop/
            relay_root = root.parent / "relay"
            if str(relay_root) not in sys.path:
                sys.path.insert(0, str(relay_root))
            from vma_relay.server import RelayServer  # noqa: PLC0415
        except Exception as exc:
            LOG.warning("cannot import vma_relay (%s) — start the relay manually", exc)
            return False
        self._relay = RelayServer(self.host, self.port, reg_token=self.reg_token)
        self._server = await asyncio.start_server(
            self._relay.handle_client, self.host, self.port)
        LOG.info("hosted VMA relay listening on %s:%s (in-process)", self.host, self.port)
        return True

    @property
    def running(self) -> bool:
        return self._server is not None and self._server.is_serving()

    async def stop(self) -> None:
        if self._server is not None:
            try:
                self._server.close()
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None
        self._relay = None
