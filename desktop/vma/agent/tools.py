"""Agent tool layer.

The reasoning model gets a fixed set of tools; the application executes them.
Filesystem access is sandboxed to the run directory, and writes are allowed
only under reports/ and exports/. No shell, no arbitrary paths.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..providers.base import ToolSpec
from ..runs.manager import Run

# --- tool specs sent to the model -------------------------------------------

SEARCH_OBSERVATIONS = ToolSpec(
    name="search_observations",
    description="Search the run's observation memory. mode='hybrid' (default) combines keyword "
                "and semantic similarity; mode='keyword' is exact-term FTS; mode='semantic' finds "
                "observations MEANINGALLY similar to the query even without shared words. Returns "
                "matching observations with id, timestamps, importance, scene and descriptions.",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Keywords or a natural-language description"},
            "mode": {"type": "string", "enum": ["hybrid", "keyword", "semantic"],
                     "description": "hybrid (default) fuses keyword+semantic ranking"},
            "start": {"type": "string", "description": "Optional ISO timestamp lower bound"},
            "end": {"type": "string", "description": "Optional ISO timestamp upper bound"},
            "importance_min": {"type": "integer", "minimum": 0, "maximum": 3,
                               "description": "Only return observations with importance >= this"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        "required": ["query"],
    },
)
GET_OBSERVATIONS_IN_RANGE = ToolSpec(
    name="get_observations_in_time_range",
    description="Return observations between two ISO timestamps, ordered by time. Use for 'what did I do between 3 and 5 PM' style questions.",
    parameters={
        "type": "object",
        "properties": {
            "start": {"type": "string"},
            "end": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 500},
        },
        "required": ["start", "end"],
    },
)
GET_OBSERVATION = ToolSpec(
    name="get_observation",
    description="Fetch one observation by id (from search results), including its full structured payload and attached media path if any.",
    parameters={
        "type": "object",
        "properties": {"observation_id": {"type": "integer"}},
        "required": ["observation_id"],
    },
)
GET_OBSERVATION_IMAGE = ToolSpec(
    name="get_observation_image",
    description="Explicitly retrieve the retained camera image for one observation. The image is "
                "attached to the conversation for direct visual inspection (only works if the "
                "reasoning model is multimodal; otherwise use inspect_frame). Call this ONLY when "
                "visual detail genuinely matters — images are never attached automatically.",
    parameters={
        "type": "object",
        "properties": {
            "observation_id": {"type": "integer", "description": "Observation whose image to retrieve"},
        },
        "required": ["observation_id"],
    },
)
GET_LOCATION_HISTORY = ToolSpec(
    name="get_location_history",
    description="Return GPS samples (lat, lon, timestamp) for the run, optionally in a time range. Use for 'where did I go' questions.",
    parameters={
        "type": "object",
        "properties": {
            "start": {"type": "string"},
            "end": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 5000},
        },
    },
)
GET_RUN_STATS = ToolSpec(
    name="get_run_stats",
    description="Run overview: time span, frame counts, observation counts, notes, reports.",
    parameters={"type": "object", "properties": {}},
)
APPEND_NOTE = ToolSpec(
    name="append_note",
    description="Append a note to the run's notes (e.g. correlations you noticed, open questions).",
    parameters={
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
)
CREATE_REPORT = ToolSpec(
    name="create_report",
    description="Write a markdown report into the run's reports/ directory. Compose the full markdown yourself from retrieved observations.",
    parameters={
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "kind": {"type": "string", "enum": ["chronological", "trip_summary", "issue_report",
                                                 "route_summary", "incident", "scene_summary", "timeline"]},
            "content_markdown": {"type": "string"},
        },
        "required": ["title", "content_markdown"],
    },
)
INSPECT_FRAME = ToolSpec(
    name="inspect_frame",
    description="EXPENSIVE: re-run the vision model on a stored frame image to answer a specific visual question (e.g. 'what did the sign say'). Only when text retrieval is not enough.",
    parameters={
        "type": "object",
        "properties": {
            "observation_id": {"type": "integer", "description": "Observation whose media to inspect"},
            "question": {"type": "string"},
        },
        "required": ["observation_id", "question"],
    },
)
READ_FILE = ToolSpec(
    name="read_file",
    description="Read a text file inside this run's directory (e.g. reports/).",
    parameters={
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Relative to the run directory"}},
        "required": ["path"],
    },
)
LIST_FILES = ToolSpec(
    name="list_files",
    description="List files in a subdirectory of this run (e.g. reports, exports).",
    parameters={
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Relative to the run directory; default '.'"}},
    },
)
WRITE_FILE = ToolSpec(
    name="write_file",
    description="Write a text file under reports/ or exports/ only.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
    },
)

ALL_TOOLS = [SEARCH_OBSERVATIONS, GET_OBSERVATIONS_IN_RANGE, GET_OBSERVATION,
             GET_OBSERVATION_IMAGE, GET_LOCATION_HISTORY, GET_RUN_STATS, APPEND_NOTE,
             CREATE_REPORT, INSPECT_FRAME, READ_FILE, LIST_FILES, WRITE_FILE]


@dataclass
class ToolContext:
    run: Run
    vision: Any = None  # VisionProvider, optional (inspect_frame disabled if None)
    embedder: Any = None  # EmbeddingProvider, optional (semantic mode disabled if None)
    max_file_bytes: int = 2_000_000

    def _resolve(self, rel: str, *, write: bool = False) -> Path:
        base = self.run.dir.resolve()
        candidate = (base / rel).resolve()
        if not candidate.is_relative_to(base):
            raise PermissionError("path escapes the run sandbox")
        if write:
            allowed_roots = [(base / "reports").resolve(), (base / "exports").resolve()]
            if not any(candidate.is_relative_to(root) for root in allowed_roots):
                raise PermissionError("writes are restricted to reports/ and exports/")
        return candidate


async def execute_tool(ctx: ToolContext, name: str, args: dict[str, Any]) -> str:
    """Execute one tool and return a JSON-string result for the model."""
    try:
        result = await _execute(ctx, name, args)
        return json.dumps({"ok": True, "result": result}, ensure_ascii=False, default=str)
    except PermissionError as exc:
        return json.dumps({"ok": False, "error": f"permission denied: {exc}"})
    except FileNotFoundError as exc:
        return json.dumps({"ok": False, "error": f"not found: {exc}"})
    except Exception as exc:  # noqa: BLE001 - report tool errors to the model
        return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"})


def _clean_obs_summary(obs: dict[str, Any]) -> dict[str, Any]:
    """Lightweight observation summary for LLM context.

    Excludes the heavy nested perception `payload` (thousands of characters of
    duplicate fields and screen text) and internal vector BLOBs. Full payload
    remains accessible through `get_observation(id)`.
    """
    clean: dict[str, Any] = {
        "id": obs["id"],
        "ts": obs["ts"],
        "local_ts": obs.get("local_ts"),
        "kind": obs.get("kind"),
        "importance": obs.get("importance"),
        "scene": obs.get("scene"),
        "summary": obs.get("summary"),
    }
    if obs.get("importance_reason"):
        clean["importance_reason"] = obs["importance_reason"]
    if "similarity" in obs:
        clean["similarity"] = obs["similarity"]
    if obs.get("kind") == "voice" and isinstance(obs.get("payload"), dict):
        clean["transcript"] = obs["payload"].get("transcript") or obs.get("summary")
    return clean


async def _execute(ctx: ToolContext, name: str, args: dict[str, Any]) -> Any:
    store = ctx.run.store
    if name == "search_observations":
        results = await _search_observations(ctx, args)
        return [_clean_obs_summary(r) for r in results]
    if name == "get_observations_in_time_range":
        results = store.observations_in_range(
            str(args["start"]), str(args["end"]), limit=int(args.get("limit") or 200)
        )
        return [_clean_obs_summary(r) for r in results]
    if name == "get_observation":
        obs = store.get_observation(int(args["observation_id"]))
        if obs is None:
            raise FileNotFoundError(f"observation {args['observation_id']}")
        obs.pop("vec", None)
        return obs
    if name == "get_observation_image":
        return await _get_observation_image(ctx, args)
    if name == "get_location_history":
        return store.location_history(args.get("start"), args.get("end"),
                                      limit=int(args.get("limit") or 1000))
    if name == "get_run_stats":
        stats = store.stats()
        stats["name"] = ctx.run.meta.get("name")
        stats["created_at"] = ctx.run.meta.get("created_at")
        return stats
    if name == "append_note":
        note_id = store.add_note(str(args["text"]), author="agent")
        return {"note_id": note_id}
    if name == "create_report":
        return _create_report(ctx, args)
    if name == "inspect_frame":
        return await _inspect_frame(ctx, args)
    if name == "read_file":
        path = ctx._resolve(str(args["path"]))
        data = path.read_bytes()
        if len(data) > ctx.max_file_bytes:
            data = data[: ctx.max_file_bytes]
        return {"path": str(args["path"]), "content": data.decode("utf-8", "replace")}
    if name == "list_files":
        base = ctx._resolve(str(args.get("path") or "."))
        if not base.exists():
            raise FileNotFoundError(str(args.get("path") or "."))
        items = []
        for p in sorted(base.rglob("*")):
            items.append({"path": str(p.relative_to(ctx.run.dir)), "size": p.stat().st_size,
                          "is_dir": p.is_dir()})
        return items[:500]
    if name == "write_file":
        path = ctx._resolve(str(args["path"]), write=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(args["content"]), encoding="utf-8")
        return {"path": str(path.relative_to(ctx.run.dir)), "bytes": len(str(args["content"]))}
    raise ValueError(f"unknown tool {name!r}")


async def _search_observations(ctx: ToolContext, args: dict[str, Any]) -> Any:
    store = ctx.run.store
    mode = str(args.get("mode") or "hybrid")
    query = str(args.get("query", ""))
    kwargs = dict(
        start=args.get("start"), end=args.get("end"),
        importance_min=int(args.get("importance_min") or 0),
        limit=int(args.get("limit") or 30),
    )
    if mode in ("semantic", "hybrid") and ctx.embedder is not None and query.strip():
        try:
            vectors = await ctx.embedder.embed([query])
        except Exception as exc:
            # Query-time degradation: a dead embedder must never error the
            # tool call — hybrid falls back to its keyword half, semantic
            # falls back to keyword search rather than returning an error.
            store.add_metric("embed_query_error", 1, {"error": str(exc)[:200]})
            return store.search_observations(query, **kwargs)
        if mode == "semantic":
            return store.semantic_search(vectors[0], **kwargs)
        return store.hybrid_search(vectors[0], query, **kwargs)
    return store.search_observations(query, **kwargs)


async def _get_observation_image(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Return the retained image for one observation; `_images_b64` is pulled
    out by the agent loop and attached to the conversation (never automatic)."""
    obs = ctx.run.store.get_observation(int(args["observation_id"]))
    if obs is None:
        raise FileNotFoundError(f"observation {args['observation_id']}")
    frame_id = obs.get("frame_id")
    frame = ctx.run.store.frame_by_id(frame_id) if frame_id else None
    media_rel = frame.get("path") if frame else None
    if not media_rel:
        raise FileNotFoundError(
            "this observation has no retained image (storage policy kept none, "
            "it was evicted, or retention expired)"
        )
    # run_media_path confines the relative path to the run dir (no traversal).
    path = ctx.run.media_path(media_rel)
    if path is None or not path.exists():
        raise FileNotFoundError("media file missing on disk")
    data = path.read_bytes()
    if len(data) > ctx.max_file_bytes:
        raise ValueError("media file exceeds the tool size cap")
    import base64

    return {
        "observation_id": obs["id"],
        "frame_path": media_rel,
        "image_attached": True,
        "note": "the JPEG is attached to this conversation turn",
        "_images_b64": [base64.b64encode(data).decode("ascii")],
    }


def _create_report(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    title = str(args["title"]).strip() or "Report"
    kind = str(args.get("kind") or "chronological")
    content = str(args["content_markdown"])
    slug = "".join(c if c.isalnum() else "_" for c in title.lower()).strip("_")[:60]
    path = ctx.run.reports_dir / f"{slug}.md"
    path.write_text(content, encoding="utf-8")
    rel = str(path.relative_to(ctx.run.dir))
    ctx.run.store.add_report(title, kind, rel, model=ctx.run.meta.get("model_config", {})
                             .get("reasoning", {}).get("model"))
    ctx.run.store.add_note(f"created report '{title}' at {rel}", author="agent")
    return {"path": rel, "bytes": len(content)}


async def _inspect_frame(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    obs = ctx.run.store.get_observation(int(args["observation_id"]))
    if obs is None:
        raise FileNotFoundError(f"observation {args['observation_id']}")
    frame_id = obs.get("frame_id")
    frame = ctx.run.store.frame_by_id(frame_id) if frame_id else None
    media_rel = frame.get("path") if frame else None
    if ctx.vision is None:
        raise RuntimeError("no vision provider configured")
    if not media_rel:
        raise FileNotFoundError("this observation has no retained image (frame was not saved)")
    image_path = ctx.run.dir / media_rel
    image_bytes = image_path.read_bytes()
    answer = await ctx.vision.inspect(image_bytes, str(args["question"]))
    return {"observation_id": obs["id"], "frame_path": media_rel, "answer": answer}


ToolExecutor = Callable[[ToolContext, str, dict[str, Any]], Awaitable[str]]
