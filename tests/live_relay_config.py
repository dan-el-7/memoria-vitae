"""Live test: relay config endpoint applies without restart.

Boots the FastAPI app (no relay configured), points it at a real in-process
relay via POST /api/config/relay, verifies the client connects and the
channel/attach secret land in status, then disables it again.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "relay"))
sys.path.insert(0, str(ROOT / "desktop"))

import uvicorn  # noqa: E402

from vma_relay.server import RelayServer  # noqa: E402

API_PORT = 18629
RELAY_PORT = 18631
BASE = f"http://127.0.0.1:{API_PORT}"


def http_json(path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data,
                                 method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


async def run_relay() -> asyncio.AbstractEventServer:
    relay = RelayServer("127.0.0.1", RELAY_PORT, reg_token="livetok")
    return await asyncio.start_server(relay.handle_client, "127.0.0.1", RELAY_PORT)


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="vma_relayapi_"))
    import os
    os.environ["VMA_DATA_DIR"] = str(tmp)
    bak = ROOT / "desktop" / "config.toml"
    saved = bak.read_text(encoding="utf-8") if bak.exists() else None
    try:
        # relay on its own thread+loop
        relay_started = threading.Event()

        def relay_thread() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            async def _run() -> None:
                server = await run_relay()
                relay_started.set()
                async with server:
                    await server.serve_forever()

            loop.run_until_complete(_run())

        threading.Thread(target=relay_thread, daemon=True).start()
        relay_started.wait(5)

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

        # 1. GET config — relay off
        cfg = http_json("/api/config/relay")
        assert cfg["relay_url"] == "", cfg
        print(f"[cfg] initial: url={cfg['relay_url']!r} status={cfg['status']}")

        # 2. POST live config -> relay client starts and registers
        r = http_json("/api/config/relay", {"relay_url": f"tcp://127.0.0.1:{RELAY_PORT}",
                                            "relay_reg_token": "livetok"})
        assert r["ok"], r
        # wait for registration
        for _ in range(50):
            st = http_json("/api/status")
            if st["relay"]["connected"]:
                break
            time.sleep(0.2)
        st = http_json("/api/status")
        assert st["relay"]["connected"], st["relay"]
        assert st["relay"]["channel_id"], st["relay"]
        assert st["relay"]["attach_secret"], "attach secret must reach the status payload"
        print(f"[cfg] live: connected channel={st['relay']['channel_id']} "
              f"attach={st['relay']['attach_secret'][:6]}…")

        # 3. GET again — reflects the saved url, token masked
        cfg = http_json("/api/config/relay")
        assert cfg["relay_url"] == f"tcp://127.0.0.1:{RELAY_PORT}", cfg
        assert cfg["relay_reg_token_set"] is True, cfg
        assert "livetok" not in json.dumps(cfg), "token must not be echoed in full"
        print(f"[cfg] get: url={cfg['relay_url']} token_set={cfg['relay_reg_token_set']}")

        # 4. persisted to config.toml
        text = (ROOT / "desktop" / "config.toml").read_text(encoding="utf-8")
        assert f"tcp://127.0.0.1:{RELAY_PORT}" in text, "config.toml must contain the relay url"
        print("[cfg] persisted to config.toml")

        # 5. disable
        r = http_json("/api/config/relay", {"relay_url": ""})
        assert r["ok"], r
        time.sleep(0.5)
        st = http_json("/api/status")
        assert not st["relay"]["connected"], st["relay"]
        cfg = http_json("/api/config/relay")
        assert cfg["relay_url"] == "", cfg
        print("[cfg] disabled live: relay disconnected, url empty")

        print("[done] RELAY CONFIG ENDPOINT PASSED")
        server.should_exit = True
        time.sleep(1)
    finally:
        if saved is not None:
            bak.write_text(saved, encoding="utf-8")


if __name__ == "__main__":
    main()
