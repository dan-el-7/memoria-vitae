"""Voice-note regressions: phone mic -> whisper -> observation memory.

A voice note is a committed observation (kind='voice') with ADDITIVE links to
temporal neighbors (+/- window minutes) and semantic neighbors (cosine >=
0.45). Links never merge or delete anything (memory pillar).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "desktop"))

from vma.app import _commit_voice_note
from vma.config import AppConfig
from vma.runs.manager import Run
from vma.stt.base import TranscriptionResult
from vma.store.db import RunStore, vec_to_bytes
from vma.utils import iso, utcnow_minus


class VecEmbedder:
    model = "fake-embed"

    async def embed(self, texts):
        out = []
        for text in texts:
            t = text.lower()
            out.append([
                1.0 if "calculator" in t else 0.0,
                1.0 if "desk" in t else 0.0,
                1.0 if "street" in t else 0.0,
            ])
        return out


def make_state(tmp_path: Path, embedder=VecEmbedder(), context_minutes: int = 2):
    run = Run("r", tmp_path / "rundir", {"model_config": {}})
    cfg = AppConfig()
    cfg.pipeline.voice_note_context_minutes = context_minutes
    return SimpleNamespace(current_run=run, embedder=embedder, cfg=cfg), run


def scene_obs(run_store: RunStore, summary: str, age_minutes: float | None,
              embed_vec: list[float] | None = None) -> int:
    frame_id = run_store.add_frame(seq=abs(hash(summary)) % 10**12, ts_server=iso(),
                                   ts_device=None, width=1, height=1, gps=None,
                                   change_score=1.0, hash_distance=1, verdict="accepted")
    ts = iso(utcnow_minus(minutes=age_minutes)) if age_minutes is not None else iso()
    obs_id = run_store.add_observation(frame_id=frame_id, ts=ts, kind="scene",
                                       summary=summary, payload={"vlm": {"summary": summary}},
                                       importance=1)
    if embed_vec is not None:
        run_store.set_observation_embedding(obs_id, len(embed_vec), "fake-embed",
                                            vec_to_bytes(embed_vec))
    return obs_id


def voice_note(text: str = "I left the calculator on the desk") -> TranscriptionResult:
    return TranscriptionResult(text=text, language="en", duration_s=3.2,
                               model="small", elapsed_ms=812)


def test_voice_note_persisted_with_temporal_link(tmp_path):
    state, run = make_state(tmp_path)
    near = scene_obs(run.store, "calculator on the desk", age_minutes=1.0)
    note = asyncio.run(_commit_voice_note(state, voice_note()))
    obs = run.store.get_observation(note["observation_id"])
    assert obs["kind"] == "voice"
    assert obs["summary"].startswith("I left the calculator")
    assert near in note["links"]
    assert note["link_counts"]["temporal"] == 1
    payload = obs["payload"]
    assert payload["transcript"] == voice_note().text
    assert payload["audio_retained"] is False


def test_temporal_window_excludes_old_observations(tmp_path):
    state, run = make_state(tmp_path, context_minutes=2)
    old = scene_obs(run.store, "calculator on the desk", age_minutes=30.0)
    note = asyncio.run(_commit_voice_note(state, voice_note()))
    assert old not in note["links"]
    assert note["link_counts"]["temporal"] == 0


def test_semantic_link_by_embedding_similarity(tmp_path):
    state, run = make_state(tmp_path)
    # 30 minutes away (outside temporal window) but semantically the same scene.
    calc = scene_obs(run.store, "calculator on the desk", age_minutes=30.0,
                     embed_vec=[1.0, 1.0, 0.0])
    street = scene_obs(run.store, "busy street crossing", age_minutes=30.0,
                       embed_vec=[0.0, 0.0, 1.0])
    note = asyncio.run(_commit_voice_note(state, voice_note("I left the calculator on the desk")))
    assert calc in note["links"], "semantic neighbor linked despite temporal distance"
    assert street not in note["links"]
    assert note["link_counts"]["semantic"] >= 1


def test_voice_note_gets_its_own_embedding(tmp_path):
    state, run = make_state(tmp_path)
    asyncio.run(_commit_voice_note(state, voice_note()))
    assert run.store.embedding_count() == 1
    # And it is retrievable via the normal semantic search path.
    hits = run.store.semantic_search([1.0, 1.0, 0.0], limit=5)
    assert hits and hits[0]["kind"] == "voice"


def test_embedder_failure_still_commits_with_temporal_links(tmp_path):
    class Broken:
        model = "broken"

        async def embed(self, texts):
            raise RuntimeError("down")

    state, run = make_state(tmp_path, embedder=Broken())
    near = scene_obs(run.store, "kitchen scene", age_minutes=1.0)
    note = asyncio.run(_commit_voice_note(state, voice_note("kitchen note")))
    obs = run.store.get_observation(note["observation_id"])
    assert obs is not None and obs["kind"] == "voice"
    assert near in note["links"]
    assert run.store.embedding_count() == 0


def test_no_audio_or_media_files_written(tmp_path):
    state, run = make_state(tmp_path)
    asyncio.run(_commit_voice_note(state, voice_note()))
    media_dir = run.dir / "media"
    files = list(media_dir.iterdir()) if media_dir.exists() else []
    assert files == [], "voice notes are memory-only: nothing may hit disk"


def test_source_recorded_in_payload(tmp_path):
    state, run = make_state(tmp_path)
    note = asyncio.run(_commit_voice_note(state, voice_note(), source="continuous"))
    obs = run.store.get_observation(note["observation_id"])
    assert obs["payload"]["source"] == "continuous"
    note2 = asyncio.run(_commit_voice_note(state, voice_note("second note")))
    obs2 = run.store.get_observation(note2["observation_id"])
    assert obs2["payload"]["source"] == "push_to_talk"  # default
