"""Pure-Python UPnP discovery + port mapping (no external deps).

SSDP M-SEARCH -> device description XML -> WANIPConnection/WANPPPConnection
control URL -> SOAP AddPortMapping. Used to open the relay port on routers
whose GUI the user cannot access (UPnP is often still enabled).
"""
from __future__ import annotations

import re
import socket
import struct
import sys
import urllib.request
import xml.etree.ElementTree as ET

SSDP_ADDR = ("239.255.255.250", 1900)
SSDP_TIMEOUT = 3.0

M_SEARCH = (
    "M-SEARCH * HTTP/1.1\r\n"
    "HOST: 239.255.255.250:1900\r\n"
    'MAN: "ssdp:discover"\r\n'
    "MX: 2\r\n"
    "ST: upnp:rootdevice\r\n"
    "\r\n"
)


def ssdp_discover(timeout: float = SSDP_TIMEOUT) -> list[str]:
    """Return device-description URLs from all responding UPnP devices."""
    urls: list[str] = []
    seen = set()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # Join multicast group on all interfaces (best effort)
    try:
        membership = socket.inet_aton(SSDP_ADDR[0]) + socket.inet_aton("0.0.0.0")
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
    except OSError:
        pass
    try:
        sock.sendto(M_SEARCH.encode(), SSDP_ADDR)
        while True:
            try:
                data, _ = sock.recvfrom(65536)
                text = data.decode("utf-8", "replace")
                m = re.search(r"LOCATION:\s*(\S+)", text, re.IGNORECASE)
                if m and m.group(1) not in seen:
                    seen.add(m.group(1))
                    urls.append(m.group(1))
            except socket.timeout:
                break
    finally:
        sock.close()
    return urls


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def find_wan_control(description_url: str) -> tuple[str, str, str] | None:
    """(control_url, service_type, friendly_name) for the WAN connection."""
    try:
        with urllib.request.urlopen(description_url, timeout=5) as resp:
            xml_text = resp.read().decode("utf-8", "replace")
    except Exception:
        return None
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    friendly = ""
    for el in root.iter():
        if _strip_ns(el.tag) == "friendlyName" and el.text:
            friendly = el.text.strip()
            break
    # Find WANIPConnection / WANPPPConnection services.
    services: list[tuple[str, str]] = []  # (service_type, control_url)
    for svc in root.iter():
        if _strip_ns(svc.tag) != "service":
            continue
        st = cu = None
        for child in svc:
            t = _strip_ns(child.tag)
            if t == "serviceType" and child.text:
                st = child.text.strip()
            elif t == "controlURL" and child.text:
                cu = child.text.strip()
        if st and cu and ("WANIPConnection" in st or "WANPPPConnection" in st):
            services.append((st, cu))
    if not services:
        return None
    st, cu = services[0]
    # Resolve relative control URL against the description URL.
    if cu.startswith("http"):
        absolute = cu
    else:
        from urllib.parse import urljoin
        absolute = urljoin(description_url, cu)
    return absolute, st, friendly


def soap_call(control_url: str, service_type: str, action: str,
              args: dict[str, str]) -> str:
    body = "".join(
        f"<m:{k}>{v}</m:{k}>" for k, v in args.items()
    )
    soap = (
        f'<?xml version="1.0"?>'
        f'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        f's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f"<s:Body><m:{action} xmlns:m=\"{service_type}\">{body}</m:{action}></s:Body>"
        f"</s:Envelope>"
    )
    req = urllib.request.Request(
        control_url, data=soap.encode("utf-8"), method="POST",
        headers={
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPACTION": f'"{service_type}#{action}"',
        },
    )
    with urllib.request.urlopen(req, timeout=8) as resp:
        return resp.read().decode("utf-8", "replace")


def get_external_ip(control_url: str, service_type: str) -> str | None:
    try:
        resp = soap_call(control_url, service_type, "GetExternalIPAddress", {})
        m = re.search(r"<NewExternalIPAddress>([^<]+)</NewExternalIPAddress>", resp)
        return m.group(1) if m else None
    except Exception:
        return None


def add_port_mapping(control_url: str, service_type: str, external_port: int,
                     internal_ip: str, internal_port: int,
                     description: str = "VMA relay",
                     lease_seconds: int = 0) -> str | None:
    """Returns None on success, or an error message."""
    args = {
        "NewRemoteHost": "",
        "NewExternalPort": str(external_port),
        "NewProtocol": "TCP",
        "NewInternalPort": str(internal_port),
        "NewInternalClient": internal_ip,
        "NewEnabled": "1",
        "NewPortMappingDescription": description,
        "NewLeaseDuration": str(lease_seconds),
    }
    try:
        soap_call(control_url, service_type, "AddPortMapping", args)
        return None
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", "replace")
        except Exception:
            detail = ""
        m = re.search(r"<errorDescription>([^<]*)</errorDescription>", detail)
        return f"HTTP {exc.code}: {m.group(1) if m else detail[:120]}"
    except Exception as exc:
        return str(exc)


def main() -> int:
    print("[upnp] SSDP discovery…")
    urls = ssdp_discover()
    if not urls:
        print("[upnp] no devices responded — UPnP unavailable on this network")
        return 1
    print(f"[upnp] {len(urls)} device(s) responded")
    for url in urls:
        found = find_wan_control(url)
        if not found:
            continue
        control_url, service_type, friendly = found
        print(f"[upnp] {friendly}: {service_type.rsplit(':', 1)[-1]}")
        ext_ip = get_external_ip(control_url, service_type)
        print(f"[upnp]   external IP: {ext_ip}")
        lan_ip = sys.argv[1] if len(sys.argv) > 1 else "192.168.1.100"
        port = int(sys.argv[2]) if len(sys.argv) > 2 else 8765
        err = add_port_mapping(control_url, service_type, port, lan_ip, port)
        if err is None:
            print(f"[upnp]   PORT FORWARD CREATED: {ext_ip}:{port} -> {lan_ip}:{port}")
            return 0
        print(f"[upnp]   mapping failed: {err}")
    print("[upnp] no usable WAN connection service found")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
