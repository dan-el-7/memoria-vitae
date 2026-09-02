"""Live check: run the real OllamaProvider.observe() with the RAISED schema on
stored frames and verify nothing is clipped at a cap (parsing alone proves
nothing — grammar truncation IS valid JSON). Requires Ollama up.
"""

import asyncio
import glob
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "desktop"))

from vma.pipeline.perceive import OBSERVATION_SCHEMA, PERCEPTION_PROMPT
from vma.providers.ollama_provider import OllamaProvider

CAPS = {"scene": 400, "summary": 900, "screen_text": 2400}
DESC_CAP = 500


def max_lengths(payload):
    # provider.observe() returns the raw VLM dict (the "vlm" wrapper is added
    # later by perceive()), so read the top-level keys.
    desc = [len(o.get("description", "")) for o in payload.get("observations", [])]
    return {
        "scene": len(payload.get("scene") or ""),
        "summary": len(payload.get("summary") or ""),
        "screen_text": len(payload.get("screen_text") or ""),
        "max_desc": max(desc) if desc else 0,
    }


async def main() -> None:
    frames = sorted(glob.glob(r"desktop\data\runs\2026-09-01_190249_h\media\*.jpg"))
    assert frames, "no stored frames to test with"
    provider = OllamaProvider("http://localhost:11434", "qwen3-vl:2b", num_ctx=8192, keep_alive=-1)
    ok = 0
    for frame in frames[:4]:
        jpeg = Path(frame).read_bytes()
        try:
            raw = await provider.observe(jpeg, PERCEPTION_PROMPT, OBSERVATION_SCHEMA)
        except Exception as exc:
            # Print the raw model content so we can see WHERE generation ended.
            detail = str(exc)
            print(f"{Path(frame).name[:24]} PARSE-FAIL: {detail[:80]}")
            import re
            import httpx
            # one-off raw call to inspect the full content tail
            payload = {
                "model": "qwen3-vl:2b",
                "messages": [{"role": "user", "content": PERCEPTION_PROMPT,
                              "images": [__import__("base64").b64encode(jpeg).decode()]}],
                "format": OBSERVATION_SCHEMA,
                "stream": False,
                "keep_alive": -1,
                "options": {"num_ctx": 8192, "temperature": 0.2},
                "think": False,
            }
            async with httpx.AsyncClient(timeout=120) as client:
                data = (await client.post("http://localhost:11434/api/chat", json=payload)).json()
            content = (data.get("message") or {}).get("content", "") or (data.get("message") or {}).get("thinking", "")
            print(f"  raw content len={len(content)} done={data.get('done')} "
                  f"eval_tokens={data.get('eval_count')} prompt_tokens={data.get('prompt_eval_count')}")
            print(f"  tail: {content[-120:]!r}")
            continue
        result_ok = bool(raw.get("summary"))
        lens = max_lengths(raw)
        at_cap = (
            lens["scene"] >= CAPS["scene"]
            or lens["summary"] >= CAPS["summary"]
            or lens["screen_text"] >= CAPS["screen_text"]
            or lens["max_desc"] >= DESC_CAP
        )
        ok += 0 if at_cap else 1
        print(
            f"{Path(frame).name[:24]} parse={'ok' if result_ok else 'BAD'} "
            f"scene={lens['scene']}/300 summary={lens['summary']}/900 "
            f"screen={lens['screen_text']}/2400 maxdesc={lens['max_desc']}/500 "
            f"{'AT-CAP' if at_cap else 'clean'}"
        )
    await provider.aclose()
    print(f"{ok}/{min(4, len(frames))} frames clean of caps")


asyncio.run(main())
