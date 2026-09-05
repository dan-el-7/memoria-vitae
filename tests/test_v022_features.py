"""v0.2.2 feature regressions: mark_moment, event segmentation,
cross-run search, at-rest encryption."""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "desktop"))

from cryptography.fernet import Fernet

from vma.agent.tools import ToolContext, execute_tool
from vma.config import PipelineConfig
from vma.pipeline.worker import PipelineWorker
from vma.store.db import MEDIA_ENC_PREFIX, RunStore, _vec_to_floats  # noqa: F401
from vma.utils import iso, utcnow_minus

# ------------------------------------------------------------------ helpers


def make_store(tmp_path: Path, name: str = "observations.db") -> RunStore:
    return RunStore(tmp_path / name)


def add_obs(store: RunStore, summary: str, scene: str | None = None, importance: int = 1,
            ts: str | None = None) -> int:
    frame_id = store.add_frame(seq=int(time.time() * 1e6) + store.stats()["frames_total"],
                               ts_server=ts or iso(), ts_device=None, width=10, height=10,
                               gps=None, change_score=1.0, hash_distance=1, verdict="accepted")
    return store.add_observation(frame_id=frame_id, ts=ts or iso(), kind="scene",
                                 scene=scene, summary=summary,
                                 payload={"vlm": {"summary": summary}}, importance=importance)


def make_worker(store: RunStore, tmp_path: Path, **cfg_kwargs) -> PipelineWorker:
    cfg = PipelineConfig(**cfg_kwargs)
    fake_vision = SimpleNamespace(observe=None, inspect=None)
    return PipelineWorker(fake_vision, store, cfg, tmp_path, device_id="dev_test")


def minutes_ago(n: float) -> str:
    return iso(utcnow_minus(minutes=n))


# ------------------------------------------------------------- mark_moment


class TestMarkMoment:
    def test_bump_importance_since(self, tmp_path):
        store = make_store(tmp_path)
        old_id = add_obs(store, "old event")
        new_id = add_obs(store, "just now")
        # backdate one row's server timestamp (insert time is always "now")
        store._conn.execute("UPDATE observations SET ts_server=? WHERE id=?",
                            (minutes_ago(10), old_id))
        store._conn.commit()
        marked = store.bump_importance_since(minutes_ago(1), min_importance=3)
        assert marked == 1
        assert store.get_observation(old_id)["importance"] == 1
        assert store.get_observation(new_id)["importance"] == 3

    def test_bump_never_lowers(self, tmp_path):
        store = make_store(tmp_path)
        add_obs(store, "already important", importance=3)
        assert store.bump_importance_since(minutes_ago(1), min_importance=3) == 0
        assert store.get_observation(1)["importance"] == 3

    def test_mark_moment_is_allowlisted(self):
        from vma.app import COMMAND_ALLOWLIST
        assert "mark_moment" in COMMAND_ALLOWLIST


# ------------------------------------------------------------------- events


class TestEvents:
    def test_consecutive_observations_join_one_event(self, tmp_path):
        store = make_store(tmp_path)
        w = make_worker(store, tmp_path, event_gap_minutes=5, event_max_minutes=30)
        base = utcnow_minus(minutes=10)
        w.track_observation(1, iso(base), "desk", 1)
        w.track_observation(2, iso(base + timedelta(minutes=1)), "desk", 2)
        w.track_observation(3, iso(base + timedelta(minutes=2)), "desk", 1)
        w.flush_event()
        events = store.events_in_range()
        assert len(events) == 1
        ev = events[0]
        assert ev["n_obs"] == 3
        assert ev["title"] == "desk"
        assert ev["rep_obs_id"] == 2  # highest importance
        assert store.stats()["events"] == 1

    def test_gap_closes_event(self, tmp_path):
        store = make_store(tmp_path)
        w = make_worker(store, tmp_path, event_gap_minutes=5, event_max_minutes=30)
        base = utcnow_minus(minutes=40)
        w.track_observation(1, iso(base), "kitchen", 1)
        w.track_observation(2, iso(base + timedelta(minutes=10)), "garage", 1)  # gap > 5m
        w.flush_event()
        events = store.events_in_range()
        assert len(events) == 2
        assert [e["title"] for e in events] == ["kitchen", "garage"]
        assert events[0]["n_obs"] == 1 and events[1]["n_obs"] == 1

    def test_max_duration_caps_event(self, tmp_path):
        store = make_store(tmp_path)
        w = make_worker(store, tmp_path, event_gap_minutes=5, event_max_minutes=30)
        base = utcnow_minus(minutes=120)
        w.track_observation(1, iso(base), "desk", 1)
        # +1m steps stay inside the gap but cross the 30m duration cap
        for i in range(1, 36):
            w.track_observation(1 + i, iso(base + timedelta(minutes=i)), "desk", 1)
        w.flush_event()
        events = store.events_in_range()
        assert len(events) >= 2  # the 60-minute run was split
        assert all(e["n_obs"] >= 1 for e in events)

    def test_get_events_tool(self, tmp_path):
        store = make_store(tmp_path)
        store.add_event(start_ts=minutes_ago(30), end_ts=minutes_ago(25),
                        title="desk work", n_obs=7, rep_obs_id=3)
        run = SimpleNamespace(dir=tmp_path, store=store, meta={"name": "t"},
                              reports_dir=tmp_path / "reports")
        out = asyncio.run(execute_tool(ToolContext(run=run), "get_events", {}))
        assert "desk work" in out and '"n_obs": 7' in out


# --------------------------------------------------------- cross-run search


class TestCrossRunSearch:
    def test_search_finds_matches_in_other_runs(self, tmp_path):
        a, b = make_store(tmp_path, "a.db"), make_store(tmp_path, "b.db")
        add_obs(a, "gradle build failed with Kotlin error", scene="terminal")
        add_obs(b, "coffee machine in the kitchen", scene="kitchen")

        def lookup():
            return [
                {"run_id": "run_a", "name": "Coding day", "db_path": str(tmp_path / "a.db")},
                {"run_id": "run_b", "name": "Morning", "db_path": str(tmp_path / "b.db")},
            ]

        run = SimpleNamespace(dir=tmp_path, store=a, meta={"name": "t"},
                              reports_dir=tmp_path / "reports")
        ctx = ToolContext(run=run, run_lookup=lookup)
        out = asyncio.run(execute_tool(ctx, "search_all_runs", {"query": "gradle"}))
        assert '"matches": 1' in out
        assert "run_a" in out and "Coding day" in out and "gradle" in out

        out2 = asyncio.run(execute_tool(ctx, "search_all_runs", {"query": "kitchen"}))
        assert "run_b" in out2

    def test_no_lookup_is_a_clean_error(self, tmp_path):
        run = SimpleNamespace(dir=tmp_path, store=make_store(tmp_path), meta={"name": "t"},
                              reports_dir=tmp_path / "reports")
        out = asyncio.run(execute_tool(ToolContext(run=run), "search_all_runs",
                                       {"query": "x"}))
        assert "not available" in out


# ------------------------------------------------------- at-rest encryption


class TestEncryption:
    def test_payload_roundtrip_and_ciphertext_at_rest(self, tmp_path):
        store = make_store(tmp_path)
        store.set_fernet(Fernet(Fernet.generate_key()))
        obs_id = add_obs(store, "screen showed the report", scene="desk")
        row = store._conn.execute("SELECT payload FROM observations WHERE id=?",
                                  (obs_id,)).fetchone()
        assert row["payload"].startswith("enc1:")  # ciphertext on disk
        obs = store.get_observation(obs_id)
        assert obs["payload"]["vlm"]["summary"] == "screen showed the report"  # plaintext in API

    def test_summary_search_survives_encryption(self, tmp_path):
        store = make_store(tmp_path)
        store.set_fernet(Fernet(Fernet.generate_key()))
        add_obs(store, "unique purple bicycle on the wall", scene="street")
        hits = store.search_observations("purple bicycle")
        assert len(hits) == 1

    def test_locked_without_key(self, tmp_path):
        store = make_store(tmp_path)
        store.set_fernet(Fernet(Fernet.generate_key()))
        obs_id = add_obs(store, "secret note", scene="desk")
        raw = store._conn.execute("SELECT payload FROM observations WHERE id=?",
                                  (obs_id,)).fetchone()["payload"]
        store.set_fernet(None)  # key gone
        obs = store.get_observation(obs_id)
        assert obs["payload"].get("encrypted") is True

    def test_media_roundtrip(self, tmp_path):
        store = make_store(tmp_path)
        store.set_fernet(Fernet(Fernet.generate_key()))
        blob = b"\xff\xd8fakejpeg" * 100
        sealed = store.encrypt_media(blob)
        assert sealed.startswith(MEDIA_ENC_PREFIX)  # marked on disk
        assert store.decrypt_media(sealed) == blob

    def test_media_locked_without_key(self, tmp_path):
        import pytest
        store = make_store(tmp_path)
        store.set_fernet(Fernet(Fernet.generate_key()))
        sealed = store.encrypt_media(b"\xff\xd8jpeg")
        store.set_fernet(None)
        with pytest.raises(PermissionError):
            store.decrypt_media(sealed)

    def test_plaintext_rows_stay_readable(self, tmp_path):
        store = make_store(tmp_path)
        obs_id = add_obs(store, "written before encryption", scene="x")  # plaintext row
        store.set_fernet(Fernet(Fernet.generate_key()))  # encryption enabled later
        assert store.get_observation(obs_id)["payload"]["vlm"]["summary"] == \
            "written before encryption"
