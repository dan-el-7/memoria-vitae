"""Schema-bound regression tests for the perception JSON schema.

With Ollama `format`, the schema becomes a decoding grammar. Unbounded arrays
or strings let a looping VLM generate until num_ctx exhaustion cuts the JSON
mid-token, which fails to parse (run 2026-09-01_180711_chennai: 10/12 frames).
Every array must carry maxItems and every string maxLength, and the schema's
worst-case output must still fit the token headroom at vision num_ctx 8192.

CAUTION from run 2026-09-01_190249_h: grammar maxLength is a hard guillotine.
The first cap set (scene 120 / summary 400 / screen_text 1200) clipped 11/11
scenes, 6/11 summaries and 5/5 screen_texts on qwen3-vl:2b — natural output
exceeded every cap. Caps must sit ~2x above measured natural output.
"""

from __future__ import annotations

from vma.pipeline.perceive import OBSERVATION_SCHEMA


def _walk(node: dict, path: str = "$"):
    """Yield (path, schema_node) for every dict in the schema tree."""
    yield path, node
    for key, value in node.items():
        if isinstance(value, dict):
            yield from _walk(value, f"{path}.{key}")
        elif isinstance(value, list):
            for i, item in enumerate(value):
                if isinstance(item, dict):
                    yield from _walk(item, f"{path}.{key}[{i}]")


def test_every_array_has_max_items() -> None:
    unbounded = [
        path for path, node in _walk(OBSERVATION_SCHEMA)
        if node.get("type") == "array" and "maxItems" not in node
    ]
    assert unbounded == [], f"unbounded arrays in schema: {unbounded}"


def test_every_string_has_max_length() -> None:
    unbounded = [
        path for path, node in _walk(OBSERVATION_SCHEMA)
        if node.get("type") == "string" and "maxLength" not in node
    ]
    assert unbounded == [], f"unbounded strings in schema: {unbounded}"


def test_worst_case_output_fits_num_ctx_headroom() -> None:
    """Worst-case schema output must stay under the num_ctx headroom.

    num_ctx 8192 (raised from 4096 after the first cap set clipped real
    output) with a ~1.3k-token prompt+image leaves ~6.9k tokens. JSON
    tokenizes at roughly 3 chars/token (dense punctuation), so the ceiling
    is ~20k chars; we hold the line at 15k for tokenizer margin. If a new
    field pushes this over, raise num_ctx in config instead of removing
    bounds — and re-measure natural output first (see the guillotine note
    in perceive.py): caps clip real data whenever they sit below it.
    """

    def max_chars(node: dict) -> int:
        if node.get("type") == "string":
            return int(node["maxLength"])
        if node.get("type") == "integer" or node.get("type") == "number":
            return 8
        if node.get("type") == "boolean":
            return 5
        if node.get("type") == "array":
            items = node["items"]
            return int(node["maxItems"]) * max_chars(items) + 2
        if node.get("type") == "object":
            props = node.get("properties", {})
            names = node.get("required", list(props))
            body = sum(len(k) + 4 + max_chars(v) for k, v in props.items() if k in names)
            optional = sum(len(k) + 4 + max_chars(v) for k, v in props.items() if k not in names)
            return body + optional + 2
        raise AssertionError(f"unhandled node at schema: type={node.get('type')}")

    worst = max_chars(OBSERVATION_SCHEMA)
    assert worst < 15000, f"schema worst case {worst} chars risks num_ctx exhaustion"
