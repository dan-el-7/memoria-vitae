"""Experiment: does repeat_penalty stop the cap-saturating loop mode?

Raw /api/chat calls with the bounded schema, default vs repeat_penalty 1.3,
twice each for stability. Spaced calls to avoid Ollama queue contention.
"""

import asyncio
import base64
import glob
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "desktop"))

import httpx

from vma.pipeline.perceive import OBSERVATION_SCHEMA, PERCEPTION_PROMPT


def sat(payload):
    scene = len(payload.get("scene") or "")
    summary = len(payload.get("summary") or "")
    desc = [len(o.get("description", "")) for o in payload.get("observations", [])]
    maxdesc = max(desc) if desc else 0
    at_cap = scene >= 400 or summary >= 900 or maxdesc >= 500
    return scene, summary, maxdesc, at_cap


async def run(rp, frames, client):
    label = f"repeat_penalty={rp}" if rp else "default       "
    saturated = 0
    for frame in frames:
        jpeg = Path(frame).read_bytes()
        options = {"num_ctx": 8192, "temperature": 0.2}
        if rp:
            options["repeat_penalty"] = rp
        payload = {
            "model": "qwen3-vl:2b",
            "messages": [{"role": "user", "content": PERCEPTION_PROMPT,
                          "images": [base64.b64encode(jpeg).decode()]}],
            "format": OBSERVATION_SCHEMA,
            "stream": False,
            "keep_alive": -1,
            "options": options,
            "think": False,
        }
        resp = await client.post("http://localhost:11434/api/chat", json=payload)
        if resp.status_code != 200 or not resp.text.strip():
            print(f"  {Path(frame).name[:22]} HTTP {resp.status_code} body={resp.text[:80]!r}")
            saturated += 1
            continue
        data = resp.json()
        msg = data.get("message") or {}
        raw = msg.get("content") or msg.get("thinking") or ""
        try:
            parsed = json.loads(raw) if not raw.strip().startswith("{") else json.loads(raw)
            scene, summary, maxdesc, at_cap = sat(parsed)
            saturated += 1 if at_cap else 0
            state = "SAT" if at_cap else "clean"
            print(f"  {Path(frame).name[:22]} scene={scene} sum={summary} desc={maxdesc} {state}")
        except Exception as exc:
            saturated += 1
            print(f"  {Path(frame).name[:22]} PARSE-FAIL len={len(raw)} {str(exc)[:50]} tail={raw[-60:]!r}")
        await asyncio.sleep(2)
    print(f">> {label}: {saturated}/{len(frames)} saturated/failed")


async def main():
    frames = sorted(glob.glob(r"desktop\data\runs\2026-09-01_190249_h\media\*.jpg"))[:4]
    async with httpx.AsyncClient(timeout=180) as client:
        await run(None, frames, client)
        await run(1.3, frames, client)
        await run(1.3, frames, client)


asyncio.run(main())
