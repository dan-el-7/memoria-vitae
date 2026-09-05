"""Hierarchical Temporal Indexing (HTI) regressions.

The hourly LLM timeline index is an additive fast path: raw observations are
never merged/deleted, the open hour is never indexed, cloud reasoning stages
are skipped (egress guard), and the agent reaches it via get_timeline_index.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "desktop"))

from vma.agent.tools import ToolContext, execute_tool
from vma.pipeline.hourly import HourlyIndexer
from vma.providers.base import ChatResult
from vma.runs.manager import Run
from vma.store.db import RunStore
from vma.utils import iso, utcnow_minus


class FakeReasoning:
    """Records prompts; returns a fixed digest."""

    model = "fake-reasoner"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def chat(self, messages, tools=None, on_stream=None) -> ChatResult:
        self.prompts.append(messages[-1].content)
        return ChatResult(content="12:01 - whiteboard erased\n12:14 - laptop opened")


def make_store(tmp_path: Path) -> RunStore:
    return RunStore(tmp_path / "observations.db")


def add_obs(store: RunStore, ts: str, summary: str, importance: int = 1) -> int:
    frame_id = store.add_frame(seq=int(time.time() * 1e6) + store.stats()["frames_total"],
                               ts_server=ts, ts_device=None, width=10, height=10,
                               gps=None, change_score=1.0, hash_distance=1, verdict="accepted")
    return store.add_observation(frame_id=frame_id, ts=ts, kind="scene", summary=summary,
                                 payload={"vlm": {"summary": summary}}, importance=importance)


def make_run(store: RunStore, tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        dir=tmp_path, store=store, meta={"name": "t"}, reports_dir=tmp_path / "reports",
    )


class TestHourIndex:
    def test_indexes_closed_hour_only(self, tmp_path):
        store = make_store(tmp_path)
        add_obs(store, iso(utcnow_minus(minutes=90)), "hour A event")   # closed hour
        add_obs(store, iso(), "current hour event")                     # open hour
        reasoning = FakeReasoning()
        ix = HourlyIndexer(reasoning, "ollama", store, enabled=True)
        built = asyncio.run(ix.build_missing())
        assert built == 1
        rows = store.hours_in_range()
        assert len(rows) == 1
        assert rows[0]["n_obs"] == 1
        assert "whiteboard" in rows[0]["summary"]
        # the open hour must never be summarized
        assert rows[0]["hour_start"].startswith(iso()[:10])
        assert rows[0]["n_obs"] != 2

    def test_disabled_or_missing_reasoning_noop(self, tmp_path):
        store = make_store(tmp_path)
        add_obs(store, iso(utcnow_minus(minutes=90)), "event")
        ix_off = HourlyIndexer(FakeReasoning(), "ollama", store, enabled=False)
        assert asyncio.run(ix_off.build_missing()) == 0
        ix_none = HourlyIndexer(None, "ollama", store, enabled=True)
        assert asyncio.run(ix_none.build_missing()) == 0
        assert store.hour_index_count() == 0

    def test_cloud_reasoning_skipped_with_metric(self, tmp_path):
        store = make_store(tmp_path)
        add_obs(store, iso(utcnow_minus(minutes=90)), "event")
        ix = HourlyIndexer(FakeReasoning(), "openai_compat", store, enabled=True)
        assert asyncio.run(ix.build_missing()) == 0
        assert store.hour_index_count() == 0
        # egress guard is recorded so the UI/user can see why nothing was built
        kinds = [r["kind"] for r in store._conn.execute(
            "SELECT kind FROM metrics").fetchall()]
        assert "hour_index_skipped_cloud" in kinds

    def test_idempotent_no_double_index(self, tmp_path):
        store = make_store(tmp_path)
        add_obs(store, iso(utcnow_minus(minutes=90)), "event")
        ix = HourlyIndexer(FakeReasoning(), "ollama", store, enabled=True)
        assert asyncio.run(ix.build_missing()) == 1
        assert asyncio.run(ix.build_missing()) == 0
        assert store.hour_index_count() == 1

    def test_prompt_carries_rows_and_caps(self, tmp_path):
        store = make_store(tmp_path)
        old = iso(utcnow_minus(minutes=90))
        for i in range(5):
            add_obs(store, old, f"event {i}")
        reasoning = FakeReasoning()
        ix = HourlyIndexer(reasoning, "ollama", store, enabled=True)
        asyncio.run(ix.build_missing())
        assert len(reasoning.prompts) == 1
        assert "event 0" in reasoning.prompts[0]
        assert "event 4" in reasoning.prompts[0]


class TestTimelineTool:
    def test_tool_returns_index_and_fallback_note(self, tmp_path):
        store = make_store(tmp_path)
        run = make_run(store, tmp_path)
        ctx = ToolContext(run=run)
        empty = asyncio.run(execute_tool(ctx, "get_timeline_index", {}))
        assert "no hourly index available" in empty

        hour = f"{iso(utcnow_minus(minutes=90))[:13]}:00:00Z"
        store.add_hour_index(hour_start=hour, summary="12:01 - thing", model="m",
                             provider="ollama", n_obs=3)
        got = asyncio.run(execute_tool(ctx, "get_timeline_index", {}))
        assert '"indexed_hours": 1' in got
        assert "12:01 - thing" in got

    def test_tool_range_filter(self, tmp_path):
        store = make_store(tmp_path)
        run = make_run(store, tmp_path)
        ctx = ToolContext(run=run)
        h1 = f"{iso(utcnow_minus(minutes=200))[:13]}:00:00Z"
        h2 = f"{iso(utcnow_minus(minutes=90))[:13]}:00:00Z"
        for h in (h1, h2):
            store.add_hour_index(hour_start=h, summary=f"digest {h}", model="m",
                                 provider="ollama", n_obs=1)
        got = asyncio.run(execute_tool(ctx, "get_timeline_index", {"start": h2, "end": h2}))
        assert '"indexed_hours": 1' in got
        assert f"digest {h2}" in got
