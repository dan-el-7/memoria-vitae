#!/usr/bin/env python
"""Synthetic phone: replays a folder of images as if a paired Android sensor.

Implements the same WebSocket protocol as the Android app:
  hello(token) -> welcome -> binary frames [u32-LE header len][JSON][JPEG]
  adapting the send interval to the ack's rec_interval_ms.

Usage:
  python tools/synthetic_phone.py --folder path/to/images --host 127.0.0.1:8619 \
      [--token TOKEN] [--code PAIRING_CODE] [--interval 1.0] [--simulate-drop]

Pairing: pass --code with a fresh code from the desktop UI (GET /api/pairing/code)
to obtain and cache a token in data/synthetic_device.json.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "desktop"))

try:
    import httpx
    import websockets
    from PIL import Image
except ImportError as exc:  # pragma: no cover
    print(f"missing dependency: {exc}\nrun: desktop\\.venv\\Scripts\\pip install httpx websockets pillow")
    raise SystemExit(1)

STATE_FILE = Path(__file__).resolve().parent.parent / "desktop" / "data" / "synthetic_device.json"


def jpeg_bytes(path: Path, max_side: int = 1024, quality: int = 70) -> tuple[bytes, int, int]:
    img = Image.open(path).convert("RGB")
    if max(img.size) > max_side:
        img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue(), img.width, img.height


def iso_utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int(time.time() * 1000) % 1000:03d}Z"


def normalize_host(raw: str) -> str:
    host = raw.strip().rstrip("/")
    for prefix in ("http://", "https://", "ws://", "wss://"):
        if host.startswith(prefix):
            return host[len(prefix):].rstrip("/")
    return host


def frame_payload(seq: int, jpeg: bytes, width: int, height: int,
                  gps: tuple[float, float] | None = None) -> bytes:
    header: dict[str, object] = {
        "seq": seq,
        "ts_device": iso_utc(),
        "w": width,
        "h": height,
    }
    if gps:
        header["gps"] = {
            "lat": gps[0], "lon": gps[1], "accuracy_m": 10.0, "ts": iso_utc()
        }
    header_bytes = json.dumps(header, separators=(",", ":")).encode("utf-8")
    return struct.pack("<I", len(header_bytes)) + header_bytes + jpeg


async def pair(host: str, code: str) -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(f"http://{normalize_host(host)}/api/pair",
                                 json={"code": code, "device_name": "synthetic-phone"})
        resp.raise_for_status()
        data = resp.json()
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(data, indent=2))
        print(f"paired: {data['device_id']} (token cached at {STATE_FILE})")
        return data


async def run(args: argparse.Namespace) -> None:
    host = normalize_host(args.host)
    token = args.token
    if not token and STATE_FILE.exists():
        token = json.loads(STATE_FILE.read_text()).get("token")
    if not token and args.code:
        token = (await pair(args.host, args.code))["token"]
    if not token:
        raise SystemExit("no token: pass --token, or --code with a fresh pairing code")

    images = sorted(p for p in Path(args.folder).iterdir()
                    if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"))
    if not images:
        raise SystemExit(f"no images in {args.folder}")
    print(f"replaying {len(images)} images from {args.folder}")

    uri = f"ws://{host}/ws/phone"
    interval = max(args.interval, 0.25)
    seq = int(time.time()) % 100_000
    sent_count = 0

    async with websockets.connect(uri, max_size=20 * 1024 * 1024) as ws:
        await ws.send(json.dumps({
            "type": "hello", "token": token,
            "device": {"model": "synthetic-phone", "app_version": "0.1.0"},
        }))
        welcome = json.loads(await ws.recv())
        if welcome.get("type") != "welcome":
            raise SystemExit(f"unexpected hello response: {welcome}")
        if not welcome.get("run_id"):
            print("ERROR: no active run on the desktop — start one first")
            return
        print(f"welcome: run {welcome['run_id']}")
        interval = max(interval, welcome.get("min_interval_ms", 250) / 1000.0)

        for img_path in images:
            jpeg, w, h = jpeg_bytes(img_path)
            seq += 1
            payload = frame_payload(seq, jpeg, w, h, args.gps)
            await ws.send(payload)
            sent_count += 1
            if not args.quiet:
                print(f"sent seq={seq} {img_path.name} ({len(jpeg) // 1024} KB)")

            # Match the Android stop-and-wait contract: do not send another
            # frame until the desktop has acknowledged this sequence number.
            ack = await wait_for_ack(ws, seq, args.ack_timeout)
            rec_ms = ack.get("rec_interval_ms")
            if isinstance(rec_ms, (int, float)) and rec_ms > 0:
                interval = max(float(rec_ms) / 1000.0, 0.25)
            if not args.quiet:
                print(f"  ack seq={seq} verdict={ack.get('verdict', '?')} "
                      f"rec={rec_ms}ms queue={ack.get('queue')}")
            await asyncio.sleep(interval)

            if args.simulate_drop and seq % 10 == 0:
                print("simulating 5s capture outage…")
                await asyncio.sleep(5)

        # Let the pipeline drain, then close.
        await asyncio.sleep(args.drain)
    print(f"done: sent {sent_count} frames")


async def wait_for_ack(ws, seq: int, timeout: float) -> dict:
    """Consume control messages until the ack for `seq` arrives."""
    while True:
        message = await asyncio.wait_for(ws.recv(), timeout=timeout)
        if isinstance(message, bytes):
            continue
        msg = json.loads(message)
        kind = msg.get("type")
        if kind == "ack" and msg.get("seq") == seq:
            return msg
        if kind == "error":
            raise RuntimeError(msg.get("message", "desktop rejected frame"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", required=True)
    parser.add_argument("--host", default="127.0.0.1:8619")
    parser.add_argument("--token", default=None)
    parser.add_argument("--code", default=None, help="fresh pairing code to auto-pair")
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--drain", type=float, default=8.0, help="seconds to wait after last frame")
    parser.add_argument("--ack-timeout", type=float, default=30.0,
                        help="seconds to wait for each desktop ack")
    parser.add_argument("--gps", nargs=2, type=float, default=None, metavar=("LAT", "LON"))
    parser.add_argument("--simulate-drop", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
