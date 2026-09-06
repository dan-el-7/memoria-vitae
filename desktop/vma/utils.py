"""Small shared helpers: time, EMA, slugs, JSON, LAN address picking."""

from __future__ import annotations

import ipaddress
import json
import re
import secrets
import socket
import string
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    """ISO-8601 UTC with milliseconds, always ending in Z."""
    dt = dt or utcnow()
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def tz_offset_string() -> str:
    """This machine's UTC offset, formatted like '+05:30'."""
    off = utcnow().astimezone().utcoffset() or timezone.utc.utcoffset(utcnow())
    total = int(off.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"{sign}{total // 3600:02d}:{(total % 3600) // 60:02d}"


def local_iso(dt: datetime | None = None) -> str:
    """The same instant as iso(), rendered in the machine's local timezone
    with a numeric offset, e.g. '2026-09-02T07:15:30.123+05:30'.

    Display-only: UTC ('...Z') remains the stored sort/range key, because
    range queries compare ISO strings and naive local strings would break
    ordering across DST/timezone changes.
    """
    dt = (dt or utcnow()).astimezone()
    return dt.isoformat(timespec="milliseconds")


def utcnow_minus(*, minutes: float = 0, hours: float = 0, days: float = 0) -> datetime:
    """Now shifted back — used for retention cutoffs."""
    from datetime import timedelta

    return utcnow() - timedelta(minutes=minutes, hours=hours, days=days)


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def run_slug(name: str, dt: datetime | None = None) -> str:
    """Directory name for a run: 2026-08-30_141502_trip_to_chennai."""
    dt = dt or utcnow()
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", name).strip("_")[:60] or "run"
    return f"{dt:%Y-%m-%d_%H%M%S}_{safe}"


def ema(prev: float | None, value: float, alpha: float = 0.3) -> float:
    return value if prev is None else alpha * value + (1 - alpha) * prev


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def random_code(n: int = 6) -> str:
    alphabet = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"  # unambiguous digits/letters
    return "".join(secrets.choice(alphabet) for _ in range(n))


def random_token(n: int = 32) -> str:
    return secrets.token_urlsafe(n)


def _subnet_rank(ip: ipaddress.IPv4Address) -> int:
    """Within one adapter tier: 192.168.x > 10.x > other private (172.16/12)."""
    octets = ip.exploded.split(".")
    if octets[0] == "192" and octets[1] == "168":
        return 0
    if octets[0] == "10":
        return 1
    return 2


# Windows adapter names are the best signal for "can the phone reach this":
# WSL/Hyper-V/VPN adapters usually own the default internet route, so a naive
# route probe prefers them — but the phone needs the Wi-Fi/Ethernet adapter.
_VIRTUAL_MARKERS = (
    "vethernet", "wsl", "hyper-v", "virtualbox", "vmware", "virtual", "loopback",
    "tailscale", "zerotier", "vpn", "warp", "cloudflare", "wireguard", "wintun",
    "tap", "tun", "docker", "bluetooth", "clash", "nord", "proxifier",
)
_PHYSICAL_MARKERS = ("wi-fi", "wifi", "wlan", "wireless", "ethernet", "eth", "lan")


def _adapter_tier(name: str) -> int:
    n = name.lower()
    if any(m in n for m in _VIRTUAL_MARKERS):
        return 50
    if any(m in n for m in _PHYSICAL_MARKERS):
        return 0
    return 10


def gather_adapter_ips() -> list[tuple[str, str]]:
    """(interface_name, ipv4) pairs via psutil; getaddrinfo fallback."""
    out: list[tuple[str, str]] = []
    try:
        import psutil

        for name, addrs in psutil.net_if_addrs().items():
            for a in addrs:
                if a.family == socket.AF_INET:
                    out.append((name, a.address))
    except ImportError:
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, family=socket.AF_INET):
                out.append((socket.gethostname(), info[4][0]))
        except OSError:
            pass
    return out


def rank_adapters(adapters: Iterable[tuple[str, str]]) -> list[str]:
    """Best-first IPv4 candidates from (interface_name, address) pairs.

    Virtual/VPN adapters are omitted entirely while any physical or
    unknown-tier candidate exists — the phone can only reach the real LAN.
    """
    scored: list[tuple[tuple[int, int, str], str]] = []
    virtual: list[tuple[tuple[int, int, str], str]] = []
    for name, address in adapters:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if ip.version != 4 or ip.is_loopback or ip.is_link_local:
            continue
        entry = ((_adapter_tier(name), _subnet_rank(ip), str(ip)), str(ip))
        if entry[0][0] >= 50:
            virtual.append(entry)
        else:
            scored.append(entry)
    ranked = [a for _, a in sorted(scored)]
    return ranked if ranked else [a for _, a in sorted(virtual)]


def rank_lan_addresses(addresses: Iterable[str]) -> list[str]:
    """Order bare IPv4 candidates (name unknown → subnet heuristic only)."""
    return rank_adapters([("", a) for a in addresses])


def probe_primary_ipv4() -> str | None:
    """The interface the OS would use for outbound traffic — no packets sent.

    A UDP socket 'connect' consults the routing table only, so this works
    even without Internet access (as long as a default route exists). NOTE:
    on VPN machines the probe follows the VPN, not the LAN — callers must
    check the owning adapter before trusting it.
    """
    for target in ("8.8.8.8", "1.1.1.1", "192.168.255.255"):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect((target, 80))
            ip = sock.getsockname()[0]
            if ip and not ipaddress.ip_address(ip).is_loopback:
                return ip
        except OSError:
            continue
        finally:
            sock.close()
    return None


def lan_address_candidates() -> tuple[str | None, list[str]]:
    """(primary, ranked): the best pairing address + all plausible ones.

    Adapter-name tiering decides; the route probe is only trusted when its
    address belongs to a non-virtual adapter (a VPN probe result is ignored).
    """
    adapters = gather_adapter_ips()
    ranked = rank_adapters(adapters)
    primary = None
    try:
        probed = probe_primary_ipv4()
    except OSError:
        probed = None
    if probed:
        tier = min((_adapter_tier(n) for n, a in adapters if a == probed), default=10)
        if tier < 50:
            primary = probed
            ranked = [probed] + [a for a in ranked if a != probed]
    return primary, ranked


def pairing_uri(host: str, code: str, online: dict[str, str] | None = None) -> str:
    """Payload encoded in the pairing QR; the Android app handles vma://pair.

    The vma:// scheme is what triggers the app's deep-link auto-pair, so it
    stays the primary payload; `url=` carries the plain http:// address for
    human readability in generic QR scanners.

    `online` (optional): {relay_host, relay_port, channel_id, attach_secret}
    — when present the phone pairs over the Internet relay instead of LAN.
    """
    from urllib.parse import quote

    uri = f"vma://pair?host={host}&code={code}&url=http://{host}"
    if online:
        uri += (f"&mode=online"
                f"&relay={quote(online.get('relay_host', ''))}"
                f"&rport={online.get('relay_port', '')}"
                f"&channel={quote(online.get('channel_id', ''))}"
                f"&attach={quote(online.get('attach_secret', ''))}")
    return uri


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))
