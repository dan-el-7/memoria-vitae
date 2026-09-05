"""Expansion-spec regressions: storage policy/budget, observation memory,
embeddings + retrieval, image-retrieval authorization, command allowlist,
platform-independent paths.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "desktop"))

from vma.agent.tools import ToolContext, execute_tool
from vma.config import PipelineConfig
from vma.pipeline.worker import PipelineWorker
from vma.runs.manager import Run
from vma.security.pairing import PairingManager
from vma.store.db import RunStore, vec_to_bytes
from vma.utils import iso, utcnow_minus

# ------------------------------------------------------------------ helpers


def make_store(tmp_path: Path) -> RunStore:
    return RunStore(tmp_path / "observations.db")


def add_media_frame(store: RunStore, ts_server: str, importance: int,
                    tmp_path: Path, size: int = 100) -> tuple[int, Path]:
    """frame row + media row + a real file; returns (obs_id, file path)."""
    frame_id = store.add_frame(seq=int(time.time() * 1e6) + store.stats()["frames_total"],
                               ts_server=ts_server, ts_device=None, width=10, height=10,
                               gps=None, change_score=1.0, hash_distance=1, verdict="accepted")
    assert frame_id is not None
    obs_id = store.add_observation(frame_id=frame_id, ts=ts_server, kind="scene",
                                   summary="s", payload={"vlm": {"summary": "s"}},
                                   importance=importance)
    rel = f"media/obs{obs_id:06d}_i{importance}_test.jpg"
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    store.set_frame_media(frame_id, rel, "digest", size)
    return obs_id, path


def make_worker(store: RunStore, tmp_path: Path, **cfg_kwargs) -> PipelineWorker:
    cfg = PipelineConfig(**cfg_kwargs)
    fake_vision = SimpleNamespace(observe=None, inspect=None)
    return PipelineWorker(fake_vision, store, cfg, tmp_path, device_id="dev_test")


# ------------------------------------------------- §4 storage policy

class TestMediaStorage:
    def test_retention_sweep_deletes_only_old_media(self, tmp_path):
        store = make_store(tmp_path)
        old_ts = iso(utcnow_minus(minutes=120))
        new_ts = iso()
        _, old_file = add_media_frame(store, old_ts, 1, tmp_path)
        _, new_file = add_media_frame(store, new_ts, 1, tmp_path)
        worker = make_worker(store, tmp_path, media_retention_minutes=60)
        worker._periodic_media_sweep()
        assert not old_file.exists()
        assert new_file.exists()
        assert store.frame_by_id(1)["path"] is None
        assert store.frame_by_id(2)["path"] is not None

    def test_budget_eviction_protects_important(self, tmp_path):
        store = make_store(tmp_path)
        base = utcnow_minus(minutes=30)
        _, boring_old = add_media_frame(store, iso(utcnow_minus(minutes=50)), 1, tmp_path)
        _, boring_new = add_media_frame(store, iso(), 1, tmp_path)
        _, important = add_media_frame(store, iso(utcnow_minus(minutes=10)), 3, tmp_path)
        worker = make_worker(store, tmp_path, media_budget_bytes=150)
        worker._enforce_media_budget()
        assert not boring_old.exists(), "oldest non-important evicted first"
        assert not boring_new.exists()
        assert important.exists(), "important images are protected"
        assert store.media_bytes_total() <= 150

    def test_no_budget_no_eviction(self, tmp_path):
        store = make_store(tmp_path)
        _, f = add_media_frame(store, iso(), 0, tmp_path)
        worker = make_worker(store, tmp_path, media_budget_bytes=0)
        worker._enforce_media_budget()
        assert f.exists()

    def test_observation_db_survives_media_eviction(self, tmp_path):
        store = make_store(tmp_path)
        obs_id, _ = add_media_frame(store, iso(), 1, tmp_path)
        worker = make_worker(store, tmp_path, media_budget_bytes=1)
        worker._enforce_media_budget()
        obs = store.get_observation(obs_id)
        assert obs is not None and obs["summary"] == "s"


# --------------------------------- §5/§6 observation memory + embeddings

class VecEmbedder:
    """Deterministic fake embedder: keyword-bag 4-dim vectors."""

    model = "fake-embed"

    def __init__(self, fail_times: int = 0) -> None:
        self.fail_times = fail_times
        self.calls = 0

    async def embed(self, texts):
        self.calls += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("embedder down")
        vecs = []
        for text in texts:
            t = text.lower()
            vecs.append([
                1.0 if "calculator" in t else 0.0,
                1.0 if "desk" in t else 0.0,
                1.0 if "street" in t else 0.0,
                1.0 if "meeting" in t else 0.0,
            ])
        return vecs


class TestEmbeddingsAndRetrieval:
    def _obs_with_vec(self, store: RunStore, summary: str, vec: list[float]) -> int:
        frame_id = store.add_frame(seq=hash(summary) % 10**12, ts_server=iso(), ts_device=None,
                                   width=1, height=1, gps=None, change_score=1.0,
                                   hash_distance=1, verdict="accepted")
        obs_id = store.add_observation(frame_id=frame_id, ts=iso(), kind="scene",
                                       summary=summary, payload={"vlm": {"summary": summary}},
                                       importance=1)
        store.set_observation_embedding(obs_id, len(vec), "test", vec_to_bytes(vec))
        return obs_id

    def test_embedding_roundtrip_and_semantic_ranking(self, tmp_path):
        store = make_store(tmp_path)
        calc = self._obs_with_vec(store, "calculator on the desk", [1, 1, 0, 0])
        street = self._obs_with_vec(store, "busy street crossing", [0, 0, 1, 0])
        meeting = self._obs_with_vec(store, "meeting with the team", [0, 0, 0, 1])
        results = store.semantic_search([1.0, 0.9, 0.0, 0.0], limit=3)
        assert [r["id"] for r in results] == [calc, street, meeting]
        assert results[0]["similarity"] > 0.99

    def test_dimension_mismatch_is_skipped(self, tmp_path):
        store = make_store(tmp_path)
        self._obs_with_vec(store, "old 3-dim model", [1, 1, 1])
        assert store.semantic_search([1.0, 0.0, 0.0, 0.0]) == []

    def test_hybrid_fuses_keyword_and_semantic(self, tmp_path):
        store = make_store(tmp_path)
        a = self._obs_with_vec(store, "calculator desk", [1, 1, 0, 0])
        b = self._obs_with_vec(store, "calculator moved", [0.9, 0.5, 0, 0])
        c = self._obs_with_vec(store, "totally unrelated", [0, 0, 1, 0])
        fused = store.hybrid_search([1.0, 1.0, 0, 0], "calculator", limit=3)
        ids = [r["id"] for r in fused]
        assert a in ids and b in ids
        assert ids[-1] == c or c not in ids  # unrelated ranks last or drops out

    def _committed_obs(self, store: RunStore, summary: str) -> int:
        frame_id = store.add_frame(seq=abs(hash(summary)) % 10**12, ts_server=iso(), ts_device=None,
                                   width=1, height=1, gps=None, change_score=1.0,
                                   hash_distance=1, verdict="accepted")
        return store.add_observation(frame_id=frame_id, ts=iso(), kind="scene",
                                     summary=summary, payload={"vlm": {"summary": summary}},
                                     importance=1)

    async def test_worker_embeds_once_per_committed_observation(self, tmp_path):
        store = make_store(tmp_path)
        worker = make_worker(store, tmp_path)
        worker.embedder = VecEmbedder()
        obs_id = self._committed_obs(store, "calculator on the desk")
        payload = {"vlm": {"scene": "desk", "summary": "calculator on the desk"}}
        await worker._embed_observation(obs_id, payload)
        await worker._embed_observation(obs_id, payload)  # same obs: idempotent insert
        assert store.embedding_count() == 1
        assert worker.embedder.calls == 2
        assert worker.status.embeddings_enabled is True

    async def test_embedder_failure_opens_circuit_breaker(self, tmp_path):
        store = make_store(tmp_path)
        worker = make_worker(store, tmp_path)
        embedder = VecEmbedder(fail_times=99)
        worker.embedder = embedder
        obs_id = self._committed_obs(store, "calculator on the desk")
        payload = {"vlm": {"summary": "calculator on the desk"}}
        for _ in range(3):
            await worker._embed_observation(obs_id, payload)
        assert worker.status.embeddings_enabled is False
        await worker._embed_observation(obs_id, payload)
        assert embedder.calls == 3, "breaker open: no further embedder calls"
        assert store.embedding_count() == 0
        # perception itself was never affected (no exception escaped)


# --------------------------------------- §8 image retrieval authorization

class TestImageRetrievalTool:
    def _run_with_image(self, tmp_path: Path, media_rel: str | None):
        store = make_store(tmp_path / "rundir")
        frame_id = store.add_frame(seq=1, ts_server=iso(), ts_device=None, width=1, height=1,
                                   gps=None, change_score=1.0, hash_distance=1, verdict="accepted")
        obs_id = store.add_observation(frame_id=frame_id, ts=iso(), kind="scene",
                                       summary="calculator", payload={}, importance=2)
        if media_rel:
            path = tmp_path / "rundir" / media_rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"\xff\xd8fakejpeg")
            store.set_frame_media(frame_id, media_rel, "d", 10)
        run = Run("r", tmp_path / "rundir", {"model_config": {}})
        return ToolContext(run=run), obs_id

    @pytest.mark.asyncio
    async def test_returns_image_with_b64_payload(self, tmp_path):
        ctx, obs_id = self._run_with_image(tmp_path, "media/obs000001_i2_t.jpg")
        raw = await execute_tool(ctx, "get_observation_image", {"observation_id": obs_id})
        parsed = json.loads(raw)
        assert parsed["ok"] is True
        assert parsed["result"]["image_attached"] is True
        assert parsed["result"]["_images_b64"]

    @pytest.mark.asyncio
    async def test_traversal_and_missing_media_rejected(self, tmp_path):
        ctx, obs_id = self._run_with_image(tmp_path, "../../outside.jpg")
        raw = await execute_tool(ctx, "get_observation_image", {"observation_id": obs_id})
        assert json.loads(raw)["ok"] is False

        ctx2, obs2 = self._run_with_image(tmp_path, None)
        raw2 = await execute_tool(ctx2, "get_observation_image", {"observation_id": obs2})
        parsed = json.loads(raw2)
        assert parsed["ok"] is False
        assert "no retained image" in parsed["error"]

    @pytest.mark.asyncio
    async def test_dead_embedder_degrades_to_keyword_not_error(self, tmp_path):
        """Query-time breaker: hybrid/semantic must fall back to keyword
        results when the embedding backend is down, never error the call."""
        class Dead:
            model = "dead"

            async def embed(self, texts):
                raise RuntimeError("embedder down")

        store = make_store(tmp_path)
        frame_id = store.add_frame(seq=7, ts_server=iso(), ts_device=None, width=1, height=1,
                                   gps=None, change_score=1.0, hash_distance=1, verdict="accepted")
        store.add_observation(frame_id=frame_id, ts=iso(), kind="scene",
                              summary="calculator on the desk", payload={}, importance=1)
        ctx = ToolContext(run=Run("r", tmp_path / "rundir2", {"model_config": {}}),
                          embedder=Dead())
        # reuse the populated store for the new run context
        ctx.run.store = store
        for mode in ("semantic", "hybrid"):
            raw = await execute_tool(ctx, "search_observations", {"query": "calculator", "mode": mode})
            parsed = json.loads(raw)
            assert parsed["ok"] is True, f"{mode} must degrade, not error"
            assert any("calculator" in (o.get("summary") or "") for o in parsed["result"])
            # Ensure lightweight summary does not leak raw vector BLOBs or the bulky payload
            for item in parsed["result"]:
                assert "vec" not in item
                assert "payload" not in item
        assert store.stats()["frames_total"] >= 1  # metric written, store usable

    @pytest.mark.asyncio
    async def test_no_auto_attach_other_tools(self, tmp_path):
        ctx, obs_id = self._run_with_image(tmp_path, "media/obs000001_i2_t.jpg")
        raw = await execute_tool(ctx, "get_observation", {"observation_id": obs_id})
        assert "_images_b64" not in json.loads(raw).get("result", {})


# ------------------------------------------ §10 command authorization

class TestCommandAuthorization:
    def test_allowlist_is_closed_and_safe(self):
        from vma.app import COMMAND_ALLOWLIST
        assert COMMAND_ALLOWLIST == {
            "get_status", "pause", "resume", "stop_run", "append_note", "chat",
            "get_observations", "get_observation_image", "list_runs", "mark_moment",
        }
        for cmd in ("shell", "exec", "eval", "run", "delete_run", ""):
            assert cmd not in COMMAND_ALLOWLIST

    @pytest.mark.asyncio
    async def test_unknown_command_rejected_before_any_state_access(self):
        from vma.app import execute_command
        with pytest.raises(ValueError):
            await execute_command(None, "format_c_drive", {})  # state untouched

    def test_bad_token_rejected_by_pairing(self, tmp_path):
        pm = PairingManager(tmp_path)
        code = pm.new_code()
        result = pm.pair(code, "test phone")
        assert result is not None
        assert pm.verify_token("forged-token") is None
        assert pm.verify_token(result["token"]) == result["device_id"]
        assert pm.verify_token(result["token"]) is not None  # reusable until revoked


# --------------------------------------- platform-independent paths

class TestPlatformPaths:
    def test_media_paths_are_posix_relative(self, tmp_path):
        store = make_store(tmp_path)
        _, path = add_media_frame(store, iso(), 2, tmp_path)
        row = store.media_rows_for_eviction()[0]
        assert "\\" not in row["path"]
        assert not Path(row["path"]).is_absolute()

    def test_run_media_path_resolves_and_confines(self, tmp_path):
        run = Run("r", tmp_path, {"model_config": {}})
        target = run.dir / "media" / "x.jpg"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x")
        assert run.media_path("media/x.jpg") == target.resolve()
        assert run.media_path("../../../etc/passwd") is None
        assert run.media_path("..\\..\\windows\\system32") is None
        assert run.media_path("media/missing.jpg") is None

    def test_data_dir_env_override(self, monkeypatch, tmp_path):
        from vma.config import AppConfig
        monkeypatch.setenv("VMA_DATA_DIR", str(tmp_path / "data"))
        cfg = AppConfig()
        assert cfg.server.data_dir == tmp_path / "data"
        assert cfg.runs_dir == tmp_path / "data" / "runs"
