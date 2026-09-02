"""Tests for pairing-address ranking and the QR deep-link payload.

The desktop used to list every adapter's IP (WSL/Hyper-V/VPN included) with no
indication of which one the phone can actually reach; these guard the ranked,
probe-first behaviour and the vma:// QR payload the Android app parses.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "desktop"))

from vma.utils import pairing_uri, probe_primary_ipv4, rank_lan_addresses
import vma.utils as vutils


def test_rank_orders_real_lans_before_virtual_adapters():
    ranked = rank_lan_addresses([
        "172.24.16.1",    # WSL / Hyper-V virtual adapter
        "192.168.1.42",   # typical home Wi-Fi
        "10.111.5.3",     # corporate / hotspot range
        "169.254.9.9",    # link-local (no DHCP)
        "127.0.0.1",      # loopback
    ])
    assert ranked == ["192.168.1.42", "10.111.5.3", "172.24.16.1"]


def test_rank_drops_non_ipv4_and_garbage():
    assert rank_lan_addresses(["not-an-ip", "::1", "fe80::1%12", ""]) == []


def test_rank_stable_within_same_class():
    assert rank_lan_addresses(["192.168.0.9", "192.168.0.2"]) == ["192.168.0.2", "192.168.0.9"]


def test_pairing_uri_format():
    # vma:// is what triggers the app's deep-link auto-pair; url= carries the
    # plain http:// address for generic QR scanners / human readability.
    assert pairing_uri("192.168.1.5:8619", "AB12CD") == \
        "vma://pair?host=192.168.1.5:8619&code=AB12CD&url=http://192.168.1.5:8619"


def test_probe_returns_none_or_a_routable_ip():
    ip = probe_primary_ipv4()
    assert ip is None or not ip.startswith("127.")


def test_rank_adapters_uses_interface_names():
    ranked = vutils.rank_adapters([
        ("vEthernet (Default Switch)", "172.27.128.9"),
        ("Wi-Fi", "10.0.0.7"),
        ("CloudflareWARP", "172.16.0.9"),
        ("Bluetooth Network Connection", "169.254.42.29"),
    ])
    assert ranked == ["10.0.0.7"]


def test_probe_ignored_when_it_belongs_to_a_virtual_adapter(monkeypatch):
    # Cloudflare WARP owns the default internet route on this machine, so the
    # UDP probe returns its address; the phone still needs the Wi-Fi adapter.
    monkeypatch.setattr(
        vutils, "gather_adapter_ips",
        lambda: [("Wi-Fi", "10.0.0.7"), ("CloudflareWARP", "172.16.0.9")],
    )
    monkeypatch.setattr(vutils, "probe_primary_ipv4", lambda: "172.16.0.9")
    primary, ranked = vutils.lan_address_candidates()
    assert primary is None
    assert ranked == ["10.0.0.7"]
