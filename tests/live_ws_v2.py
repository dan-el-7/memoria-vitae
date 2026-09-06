"""Live smoke test: actual FastAPI server + WS phone with CR+E2E auth.

Starts uvicorn in-process on an ephemeral port, then plays a full v2 phone:
pair over HTTP -> WS connect -> challenge-response -> e2e -> sealed frame.

Run: desktop/.venv/Scripts/python tests/live_ws_v2.py
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "desktop"))

import uvicorn  # noqa: E402

PORT = 18619
BASE = f"http://127.0.0.1:{PORT}"


def http_json(path: str, payload: dict | None = None, method: str | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method or ("POST" if data else "GET"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="vma_ws_"))
    (ROOT / "desktop" / "config.toml").rename(ROOT / "desktop" / "config.toml.bak")
    import os
    os.environ["VMA_DATA_DIR"] = str(tmp)
    try:
        import vma.app as vma_app
        from vma.security.auth_crypto import (
            DIR_PHONE_TO_SERVER, DIR_SERVER_TO_PHONE, SealedChannel,
            challenge_response, derive_session_key,
        )

        config = uvicorn.Config(vma_app.app, host="127.0.0.1", port=PORT, log_level="warning")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        for _ in range(50):
            try:
                http_json("/api/health")
                break
            except Exception:
                time.sleep(0.2)
        print("[server] up")

        code = http_json("/api/pairing/code")["code"]
        print(f"[pair] code {code}")
        paired = http_json("/api/pair", {"code": code, "device_name": "WS Smoke Phone"})
        assert paired.get("cr_secret"), paired
        print(f"[pair] device {paired['device_id']} (cr secret issued)")

        async def phone_ws() -> None:
            import websockets

            uri = f"ws://127.0.0.1:{PORT}/ws/phone"
            async with websockets.connect(uri) as ws:
                import secrets as pysecrets
                client_nonce = pysecrets.token_urlsafe(12)
                hello = {"type": "hello", "token": paired["token"],
                         "device": {"model": "smoke"}, "cr": {"nonce": client_nonce}}
                await ws.send(json.dumps(hello))
                challenge = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                assert challenge["type"] == "auth_challenge", challenge
                resp = challenge_response(paired["cr_secret"], challenge["nonce"], client_nonce)
                await ws.send(json.dumps({"type": "auth_response",
                                          "nonce": client_nonce, "response": resp}))
                e2e = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                assert e2e["type"] == "e2e_start", e2e
                seal = SealedChannel(derive_session_key(
                    paired["cr_secret"], challenge["nonce"], client_nonce))
                # welcome arrives sealed (hex text)
                welcome_raw = await asyncio.wait_for(ws.recv(), timeout=10)
                welcome = json.loads(seal.unseal(bytes.fromhex(welcome_raw), DIR_SERVER_TO_PHONE))
                assert welcome["type"] == "welcome", welcome
                assert welcome["auth"] == "cr+e2e", welcome
                print(f"[phone] welcome auth={welcome['auth']} run={welcome['run_id']}")

                # sealed frame (BINARY message) -> sealed ack
                header = json.dumps({"seq": 5, "ts_device": "2026-09-07T12:00:00.000Z",
                                     "w": 8, "h": 8}).encode()
                payload = len(header).to_bytes(4, "little") + header + b"\xff\xd8FAKE\xff\xd9"
                await ws.send(seal.seal(payload, DIR_PHONE_TO_SERVER))
                ack_raw = await asyncio.wait_for(ws.recv(), timeout=10)
                ack = json.loads(seal.unseal(bytes.fromhex(ack_raw), DIR_SERVER_TO_PHONE))
                assert ack["type"] == "ack" and ack["verdict"] == "dropped_no_run", ack
                print(f"[phone] sealed frame acked: {ack['verdict']}")

                # plaintext probe after e2e must be rejected
                await ws.send(json.dumps({"type": "heartbeat"}))
                err_raw = await asyncio.wait_for(ws.recv(), timeout=10)
                # error frames go through the sealed channel too now
                try:
                    err = json.loads(seal.unseal(bytes.fromhex(err_raw), DIR_SERVER_TO_PHONE))
                except Exception:
                    err = json.loads(err_raw)
                assert err.get("type") == "error" and err.get("code") == "e2e_bad", err
                print("[phone] plaintext-after-e2e correctly rejected")

        asyncio.run(phone_ws())

        status = http_json("/api/status")
        assert status["sensor"]["auth"] == "cr+e2e", status["sensor"]
        print(f"[status] sensor auth={status['sensor']['auth']} "
              f"discovery={status.get('discovery', {}).get('advertised')}")
        print("[done] WS SMOKE PASSED")
        server.should_exit = True
        thread.join(timeout=10)
    finally:
        bak = ROOT / "desktop" / "config.toml.bak"
        if bak.exists():
            bak.rename(ROOT / "desktop" / "config.toml")


if __name__ == "__main__":
    main()
