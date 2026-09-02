"""Unit tests: change detection, intake backpressure, store, sandbox."""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "desktop"))

from PIL import Image

from vma.pipeline.change import ChangeDetector, dhash, hamming
from vma.pipeline.intake import Frame, FrameIntake
from vma.store.db import RunStore
from vma.agent.tools import ToolContext, execute_tool


def make_jpeg(color: tuple[int, int, int], size: tuple[int, int] = (320, 240),
              noise_seed: int | None = None) -> bytes:
    img = Image.new("RGB", size, color)
    if noise_seed is not None:
        import random
        rng = random.Random(noise_seed)
        px = img.load()
        for _ in range(2000):
            x, y = rng.randrange(size[0]), rng.randrange(size[1])
            px[x, y] = (rng.randrange(256), rng.randrange(256), rng.randrange(256))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


# ------------------------------------------------------------------ change

class TestChangeDetection:
    def test_identical_frames_no_change(self):
        det = ChangeDetector()
        jpeg = make_jpeg((120, 30, 30))
        det.evaluate(jpeg)
        changed, mad, dist = det.evaluate(jpeg)
        assert not changed
        assert mad < 6 and dist < 6

    def test_different_scene_changes(self):
        det = ChangeDetector()
        det.evaluate(make_jpeg((10, 10, 10)))
        changed, mad, dist = det.evaluate(make_jpeg((240, 240, 240)))
        assert changed
        assert mad > 6 or dist > 6

    def test_noisy_same_scene_below_threshold(self):
        det = ChangeDetector()
        det.evaluate(make_jpeg((100, 100, 100)))
        changed, _, _ = det.evaluate(make_jpeg((100, 100, 100), noise_seed=7))
        assert not changed, "minor noise should not trigger VLM inference"

    def test_detector_state_only_advances_on_change(self):
        det = ChangeDetector()
        a = make_jpeg((10, 10, 10))
        b = make_jpeg((250, 250, 250))
        det.evaluate(a)
        det.evaluate(b)                     # a -> b: change, reference := b
        changed, _, _ = det.evaluate(make_jpeg((10, 10, 10)))
        assert changed                      # b -> a is also a change

    def test_dhash_stable(self):
        jpeg = make_jpeg((80, 80, 80))
        assert dhash(jpeg) == dhash(jpeg)
        assert hamming(dhash(jpeg), dhash(jpeg)) == 0


# ------------------------------------------------------------------ intake

class TestIntakeBackpressure:
    async def test_duplicate_seq_rejected(self):
        intake = FrameIntake(capacity=3)
        f1 = Frame(seq=1, ts_device=None, jpeg=b"x")
        assert await intake.put(f1) == "accepted"
        f1_again = Frame(seq=1, ts_device=None, jpeg=b"x")
        assert await intake.put(f1_again) == "duplicate"

    async def test_latest_wins_on_overflow(self):
        intake = FrameIntake(capacity=2)
        for seq in (1, 2):
            await intake.put(Frame(seq=seq, ts_device=None, jpeg=b"x"))
        verdict = await intake.put(Frame(seq=3, ts_device=None, jpeg=b"x"))
        assert verdict == "stale_dropped"
        assert intake.stats.dropped_stale == 1
        got = [await intake.get() for _ in range(2)]
        assert [f.seq for f in got] == [2, 3]

    async def test_queue_never_exceeds_capacity(self):
        intake = FrameIntake(capacity=2)
        for seq in range(20):
            await intake.put(Frame(seq=seq, ts_device=None, jpeg=b"x"))
        assert intake.qsize() <= 2
        assert intake.stats.dropped_stale >= 17


# ------------------------------------------------------------------- store

class TestRunStore:
    @pytest.fixture()
    def store(self, tmp_path: Path) -> RunStore:
        return RunStore(tmp_path / "run" / "observations.db")

    def test_frame_and_observation_roundtrip(self, store: RunStore):
        fid = store.add_frame(seq=5, ts_server="2026-08-30T10:00:00Z", ts_device=None,
                              width=640, height=480, gps={"lat": 12.9, "lon": 80.2},
                              change_score=9.1, hash_distance=8, verdict="accepted")
        obs_id = store.add_observation(
            frame_id=fid, ts="2026-08-30T10:00:00Z", kind="scene", summary="street with traffic",
            payload={"vlm": {"scene": "street", "summary": "street with traffic",
                             "observations": [], "actions": [], "issues": []}},
            importance=2, scene="street", confidence=0.9,
        )
        got = store.get_observation(obs_id)
        assert got["summary"] == "street with traffic"
        assert got["payload"]["vlm"]["scene"] == "street"

    def test_duplicate_seq_returns_none(self, store: RunStore):
        first = store.add_frame(seq=1, ts_server="t", ts_device=None, width=1, height=1,
                                gps=None, change_score=None, hash_distance=None, verdict="accepted")
        dup = store.add_frame(seq=1, ts_server="t", ts_device=None, width=1, height=1,
                              gps=None, change_score=None, hash_distance=None, verdict="accepted")
        assert first is not None and dup is None

    def test_time_range_query(self, store: RunStore):
        for i, ts in enumerate(["2026-08-30T14:00:00Z", "2026-08-30T15:30:00Z", "2026-08-30T16:45:00Z"]):
            store.add_observation(frame_id=None, ts=ts, kind="scene", summary=f"obs {i}",
                                  payload={}, importance=1)
        rows = store.observations_in_range("2026-08-30T15:00:00Z", "2026-08-30T16:00:00Z")
        assert [r["summary"] for r in rows] == ["obs 1"]

    def test_fts_search(self, store: RunStore):
        store.add_observation(frame_id=None, ts="2026-08-30T14:00:00Z", kind="scene",
                              summary="entered a large shopping mall",
                              payload={}, importance=2)
        store.add_observation(frame_id=None, ts="2026-08-30T15:00:00Z", kind="scene",
                              summary="walking along the beach road",
                              payload={}, importance=1)
        hits = store.search_observations("mall")
        assert len(hits) == 1 and "mall" in hits[0]["summary"]

    def test_stats(self, store: RunStore):
        store.add_frame(seq=1, ts_server="t", ts_device=None, width=1, height=1, gps=None,
                        change_score=None, hash_distance=None, verdict="accepted")
        store.add_frame(seq=2, ts_server="t", ts_device=None, width=1, height=1, gps=None,
                        change_score=None, hash_distance=None, verdict="nochange")
        s = store.stats()
        assert s["frames_total"] == 2 and s["frames_accepted"] == 1 and s["frames_nochange"] == 1


# ---------------------------------------------------------------- sandbox

class TestToolSandbox:
    @pytest.fixture()
    def ctx(self, tmp_path: Path) -> ToolContext:
        (tmp_path / "reports").mkdir()
        (tmp_path / "exports").mkdir()
        (tmp_path / "secret.txt").write_text("top secret", encoding="utf-8")

        class FakeRun:
            id = "test_run"
            dir = tmp_path
            meta = {"name": "test", "model_config": {}}

            class reports_dir_property:
                pass

        import types
        run = types.SimpleNamespace(id="test_run", dir=tmp_path,
                                    meta={"name": "test", "model_config": {}},
                                    store=RunStore(tmp_path / "observations.db"),
                                    reports_dir=tmp_path / "reports",
                                    exports_dir=tmp_path / "exports")
        return ToolContext(run=run)

    async def test_write_restricted_to_reports_exports(self, ctx: ToolContext):
        ok = await execute_tool(ctx, "write_file", {"path": "reports/x.md", "content": "# hi"})
        assert '"ok": true' in ok
        denied = await execute_tool(ctx, "write_file", {"path": "../escape.md", "content": "x"})
        assert '"ok": false' in denied
        denied2 = await execute_tool(ctx, "write_file", {"path": "secret.txt", "content": "x"})
        assert '"ok": false' in denied2

    async def test_read_cannot_escape_run_dir(self, ctx: ToolContext):
        result = await execute_tool(ctx, "read_file", {"path": "../../etc/passwd"})
        assert '"ok": false' in result
        result = await execute_tool(ctx, "read_file", {"path": "secret.txt"})
        assert "top secret" in result  # inside the run dir is readable

    async def test_unknown_tool_reported(self, ctx: ToolContext):
        result = await execute_tool(ctx, "rm_rf", {})
        assert '"ok": false' in result
