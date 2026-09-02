"""Perception stage: image -> schema-constrained structured observation.

The VLM only describes what it sees; temporal context (timestamps, GPS,
sequence, links to the previous observation) is attached by this stage, not
hallucinated by the model. The JSON schema is enforced by Ollama's `format`
parameter — deliberately *without* tools in the same request (ollama#8095).
"""

from __future__ import annotations

from typing import Any

from ..providers.base import VisionProvider
from ..utils import iso

OBSERVATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["scene", "summary", "observations", "importance"],
    "properties": {
        "scene": {"type": "string", "maxLength": 400,
                  "description": "Short label for the place/surface, e.g. 'street', 'car interior', 'kitchen'"},
        "summary": {"type": "string", "maxLength": 900,
                    "description": "One-sentence summary of what is happening"},
        "observations": {
            "type": "array",
            "maxItems": 5,
            "items": {
                "type": "object",
                "required": ["description", "confidence"],
                "properties": {
                    "description": {"type": "string", "maxLength": 500},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "entities": {"type": "array", "maxItems": 4,
                                 "items": {"type": "string", "maxLength": 60},
                                 "description": "Named things present: people roles, vehicles, objects, signs"},
                    "kind": {"type": "string", "maxLength": 40,
                             "description": "object|person|vehicle|animal|text|activity|environment"},
                },
            },
        },
        "actions": {
            "type": "array",
            "maxItems": 3,
            "items": {
                "type": "object",
                "required": ["description"],
                "properties": {
                    "description": {"type": "string", "maxLength": 400},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
            },
        },
        "issues": {
            "type": "array",
            "maxItems": 3,
            "items": {
                "type": "object",
                "required": ["severity", "description"],
                "properties": {
                    "severity": {"type": "string", "maxLength": 20, "enum": ["info", "warning", "critical"]},
                    "description": {"type": "string", "maxLength": 400},
                },
            },
        },
        "screen_text": {"type": "string", "maxLength": 2400,
                        "description": "Any legible text/signs/display content, empty if none"},
        "importance": {"type": "integer", "minimum": 0, "maximum": 3,
                       "description": "0 static/boring, 1 routine, 2 notable, 3 critical/safety"},
        "importance_reason": {"type": "string", "maxLength": 300},
        "continues_previous": {"type": "boolean",
                               "description": "True if this looks like the same scene/activity as just before"},
    },
}
# Every array and string above is bounded ON PURPOSE: with Ollama `format`, the
# schema becomes a decoding grammar, and unbounded arrays/strings let a looping
# VLM generate until num_ctx is exhausted — the JSON is cut mid-token and fails
# to parse (run 2026-09-01_180711_chennai: 10/12 frames errored this way).
# VERIFIED GOTCHA (run 2026-09-01_190249_h): grammar maxLength is a GUILLOTINE,
# not a style guide — the first cap set (scene 120, summary 400, screen_text
# 1200) clipped 11/11 scenes, 6/11 summaries and 5/5 screen_texts mid-sentence
# on this very model. qwen3-vl:2b naturally writes scenes >120 chars and
# screen transcriptions >1200. Caps must therefore sit ~2x above MEASURED
# natural output: they are loop insurance, never brevity enforcement. When a
# cap clips real data, raise the cap (and num_ctx if the worst case grows),
# never "tighten the model". num_ctx vision 8192 keeps even the degenerate
# worst case (~10.5k chars ~= 3.5k tokens) inside context with margin.

PERCEPTION_PROMPT = """You are the perception stage of a continuous visual memory system.
You receive ONE frame captured by a phone camera. Describe it factually for later retrieval.

Rules:
- Describe only what is visible. Never invent timestamps, locations or events you cannot see.
- confidence in [0,1]: 0.9+ = certain, 0.5-0.9 = likely, <0.5 = uncertain guess.
- importance: 0 for a static scene with nothing new, 1 for routine activity,
  2 for notable events (meeting people, entering places, purchases, weather),
  3 for safety-relevant or highly unusual content.
- If text is legible (signs, screens, labels), transcribe it into screen_text.
- Keep it short: 1 sentence summary, up to 5 observations, up to 3 actions.
"""

REQUIRED_KEYS = {"scene", "summary", "observations", "importance"}


class PerceptionResult:
    __slots__ = ("payload", "latency_ms", "model", "provider")

    def __init__(self, payload: dict[str, Any], latency_ms: int, model: str, provider: str) -> None:
        self.payload = payload
        self.latency_ms = latency_ms
        self.model = model
        self.provider = provider


async def perceive(vision: VisionProvider, jpeg: bytes, *, context: dict[str, Any] | None = None) -> PerceptionResult:
    """Run the VLM on one frame; validate; raise ValueError on unusable output."""
    prompt = PERCEPTION_PROMPT
    if context:
        ctx_lines = []
        if context.get("previous_summary"):
            ctx_lines.append(f"Previous scene summary: {context['previous_summary']}")
        if ctx_lines:
            prompt += "\nContext (for continuity, not to be repeated):\n- " + "\n- ".join(ctx_lines)

    raw = await vision.observe(jpeg, prompt, OBSERVATION_SCHEMA)
    meta = raw.pop("_meta", {})
    missing = REQUIRED_KEYS - raw.keys()
    if missing:
        raise ValueError(f"VLM observation missing keys: {sorted(missing)}")
    raw.setdefault("actions", [])
    raw.setdefault("issues", [])
    raw.setdefault("observations", [])
    raw["importance"] = max(0, min(3, int(raw.get("importance", 1))))

    envelope: dict[str, Any] = {
        "schema_version": 1,
        "timestamp": context.get("ts_device") or iso() if context else iso(),
        "source": {
            "device_id": (context or {}).get("device_id"),
            "frame_seq": (context or {}).get("seq"),
            "pipeline": "phone_camera->vlm",
        },
        "location": (context or {}).get("gps"),
        "vlm": raw,
        "recorded_at": iso(),
    }
    return PerceptionResult(envelope, int(meta.get("total_ms", 0)), meta.get("model", "?"),
                            meta.get("provider", "?"))


def flatten_observation(payload: dict[str, Any]) -> str:
    """One-line human-readable digest used for previous-context and reports."""
    vlm = payload.get("vlm") or {}
    parts = [vlm.get("scene", ""), vlm.get("summary", "")]
    for action in vlm.get("actions", [])[:2]:
        parts.append(action.get("description", ""))
    return " | ".join(p for p in parts if p)


def issue_count(payload: dict[str, Any]) -> int:
    vlm = payload.get("vlm") or {}
    return len(vlm.get("issues") or [])


def to_report_line(payload: dict[str, Any]) -> str:
    ts = payload.get("timestamp", "?")
    loc = payload.get("location") or {}
    where = f" @ {loc.get('lat'):.5f},{loc.get('lon'):.5f}" if loc.get("lat") and loc.get("lon") else ""
    vlm = payload.get("vlm") or {}
    line = f"- **{ts}**{where} — {vlm.get('summary', '')}"
    issues = vlm.get("issues") or []
    for issue in issues:
        line += f"  ⚠ [{issue.get('severity', 'info')}] {issue.get('description', '')}"
    return line
