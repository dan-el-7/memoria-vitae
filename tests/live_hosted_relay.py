"""Live test: local relay URL -> the app hosts the relay in-process.

Boots the FastAPI app configured with relay_url=tcp://127.0.0.1:<port>
(NOTHING else listening there), then verifies:
  1. the app hosted the relay (status.relay.hosted == True)
  2. the desktop's relay client registered through it (connected, channel)
  3. an external phone can attach through the hosted relay end-to-end
     (relay attach-secret flow over the real socket)
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "desktop"))
sys.path.insert(0, str(ROOT / "relay"))

import uvicorn  # noqa: E402

API_PORT = 18639
RELAY_PORT = 18641
BASE = f"http://127.0.0.1:{API_PORT}"


def http_json(path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data,
                                 method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def phone_attach_probe(port: int, channel: str, attach: str) -> bool:
    """Attach as a phone to the hosted relay (handshake only)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    hello = {"role": "phone", "channel_id": channel, "attach_secret": attach,
             "wait_for_desktop": False}
    writer.write(json.dumps(hello).encode() + b"\n")
    await writer.drain()
    line = await asyncio.wait_for(reader.readline(), timeout=5)
    writer.close()
    resp = json.loads(line.decode())
    return resp.get("type") == "attached"


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="vma_hosted_"))
    import os
    os.environ["VMA_DATA_DIR"] = str(tmp)
    os.environ["VMA_RELAY_URL"] = f"tcp://127.0.0.1:{RELAY_PORT}"
    os.environ["VMA_RELAY_REG_TOKEN"] = "hostedtok"
    bak = ROOT / "desktop" / "config.toml"
    saved = bak.read_text(encoding="utf-8") if bak.exists() else None

    try:
        import vma.app as vma_app
        config = uvicorn.Config(vma_app.app, host="127.0.0.1", port=API_PORT, log_level="warning")
        server = uvicorn.Server(config)
        threading.Thread(target=server.run, daemon=True).start()
        for _ in range(50):
            try:
                http_json("/api/health")
                break
            except Exception:
                time.sleep(0.2)
        print("[server] up")

        # 1+2: hosted relay + client registered through it
        for _ in range(50):
            st = http_json("/api/status")
            if st["relay"]["connected"]:
                break
            time.sleep(0.2)
        st = http_json("/api/status")
        assert st["relay"]["hosted"], f"relay not hosted: {st['relay']}"
        assert st["relay"]["connected"], f"relay client not connected: {st['relay']}"
        assert st["relay"]["channel_id"], st["relay"]
        assert st["relay"]["attach_secret"], st["relay"]
        print(f"[hosted] relay hosted in-app: connected channel={st['relay']['channel_id']} "
              f"attach={st['relay']['attach_secret'][:6]}…")

        # 3: external "phone" attaches through the hosted relay
        ok = asyncio.run(phone_attach_probe(RELAY_PORT, st["relay"]["channel_id"],
                                            st["relay"]["attach_secret"]))
        assert ok, "phone could not attach through the hosted relay"
        print("[hosted] external phone attach through in-process relay: OK")

        # 4: the pair request flow still works over HTTP (dashboard approval)
        req = http_json("/api/pair/request", {"device_name": "hosted-test-phone"})
        assert req.get("request_id"), req
        pending = http_json("/api/pair/requests")
        assert any(r["request_id"] == req["request_id"] for r in pending), pending
        appr = http_json("/api/pair/approve", {"request_id": req["request_id"]})
        assert appr.get("code"), appr
        print("[hosted] pairing request -> approve -> code issued: OK")

        print("[done] HOSTED RELAY PASSED")
        server.should_exit = True
        time.sleep(1)
    finally:
        if saved is not None:
            bak.write_text(saved, encoding="utf-8")


if __name__ == "__main__":
    main()
