"""mDNS/zeroconf LAN discovery: advertise the desktop so phones on the same
Wi-Fi can find it without typing IP addresses.

Service type ``_vma._tcp.local.`` with a TXT record carrying:

- ``v``    protocol version (currently 1)
- ``id``   stable instance id (desktop fingerprint, changes only with data dir)
- ``name`` human-readable desktop name (host name)
- ``pair`` "1" while a pairing code is live (phone can show "ready to pair")

zeroconf is optional: if the package is missing or advertisement fails (no
multicast on this network), the server keeps running — pairing still works by
QR/IP. Android discovers via its built-in NsdManager (no new app deps).
"""

from __future__ import annotations

import logging
from typing import Any

LOG = logging.getLogger("vma.discovery")

SERVICE_TYPE = "_vma._tcp.local."
PROTOCOL_VERSION = 1


class DiscoveryAdvertiser:
    """Owns the zeroconf registration lifecycle; safe to use as a no-op."""

    def __init__(self, port: int, desktop_name: str, instance_id: str) -> None:
        self.port = port
        self.desktop_name = desktop_name or "VMA Desktop"
        self.instance_id = instance_id
        self._zc: Any = None
        self._info: Any = None
        self.pairing_live = False  # UI flips this when a code is generated

    def start(self) -> bool:
        try:
            import socket
            from zeroconf import ServiceInfo, Zeroconf
            infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
            local_ips = list({i[4][0] for i in infos if "." in i[4][0]})
            if not local_ips:
                local_ips = ["127.0.0.1"]
            self._zc = Zeroconf()
            self._info = ServiceInfo(
                SERVICE_TYPE,
                f"{self.instance_id}.{SERVICE_TYPE}",
                addresses=[socket.inet_aton(ip) for ip in local_ips],
                port=self.port,
                properties=self._properties(),
                server=f"{self.instance_id}.local.",
            )
            self._zc.register_service(self._info, ttl=60)  # type: ignore[arg-type]
            LOG.info("mDNS advertisement live: %s.local.:%s (%s)",
                     self.instance_id, self.port, self.desktop_name)
            return True
        except Exception as exc:
            LOG.warning("mDNS advertisement failed (%s) — pairing by IP/QR still works", exc)
            self._zc = None
            return False

    def set_pairing_live(self, live: bool) -> None:
        self.pairing_live = live
        self._update()

    def _properties(self) -> dict[str, str]:
        return {
            "v": str(PROTOCOL_VERSION),
            "id": self.instance_id,
            "name": self.desktop_name[:60],
            "pair": "1" if self.pairing_live else "0",
        }

    def _update(self) -> None:
        if self._zc is None or self._info is None:
            return
        try:
            self._info.properties = self._properties()
            self._zc.update_service(self._info)  # type: ignore[arg-type]
        except Exception as exc:
            LOG.debug("mDNS update failed: %s", exc)

    def stop(self) -> None:
        if self._zc is not None:
            try:
                self._zc.unregister_service(self._info)
                self._zc.close()
            except Exception:
                pass
            self._zc = None


def desktop_instance_id(data_dir: Any) -> str:
    """Stable per-installation id derived from the data dir path (used as the
    mDNS instance name so a phone remembers this desktop across restarts)."""
    import hashlib
    import platform

    raw = f"{platform.node()}|{data_dir}".encode("utf-8")
    return "vma-" + hashlib.sha256(raw).hexdigest()[:10]
