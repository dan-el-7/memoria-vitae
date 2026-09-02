"""Local-time regressions: UTC stays the stored sort key; every observation
carries a machine-local `local_ts` for display; legacy run DBs are migrated
and backfilled on open.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "desktop"))

from vma.runs.manager import Run
from vma.store.db import RunStore
from vma.utils import iso, local_iso, parse_iso, tz_offset_string, utcnow

UTC = timezone.utc


def test_local_iso_same_instant_with_offset():
    utc_text = iso()
    local_text = local_iso(parse_iso(utc_text))
    assert parse_iso(local_text) == parse_iso(utc_text), "local must be the same instant"
    assert local_text[-3] != "Z", "local must not be Z-suffixed"
    assert local_text[-6] in ("+", "-"), f"local must carry a numeric offset: {local_text}"


def test_tz_offset_string_format():
    offset = tz_offset_string()
    assert offset[0] in ("+", "-") and offset[3] == ":" and len(offset) == 6
    datetime.strptime(offset, "%z")  # parses as a real offset


def test_add_observation_fills_local_ts(tmp_path):
    store = RunStore(tmp_path / "db.sqlite")
    frame_id = store.add_frame(seq=1, ts_server=iso(), ts_device=None, width=1, height=1,
                               gps=None, change_score=1.0, hash_distance=1, verdict="accepted")
    known_utc = "2026-09-01T14:30:15.123Z"
    obs_id = store.add_observation(frame_id=frame_id, ts=known_utc, kind="scene",
                                   summary="s", payload={"vlm": {"summary": "s"}}, importance=1)
    obs = store.get_observation(obs_id)
    assert obs["local_ts"] is not None
    assert parse_iso(obs["local_ts"]) == parse_iso(known_utc), "same instant, local rendering"
    assert obs["local_ts"].endswith(tz_offset_string()), f"machine offset {tz_offset_string()}"


def test_legacy_db_is_migrated_and_backfilled(tmp_path):
    """A pre-local_ts run DB gains the column and gets backfilled on open."""
    db_path = tmp_path / "observations.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """CREATE TABLE observations (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               frame_id INTEGER, ts TEXT NOT NULL, ts_server TEXT NOT NULL,
               kind TEXT NOT NULL DEFAULT 'scene', scene TEXT, summary TEXT NOT NULL,
               importance INTEGER NOT NULL DEFAULT 1, importance_reason TEXT,
               confidence REAL, payload TEXT NOT NULL, model TEXT, provider TEXT,
               latency_ms INTEGER)"""
    )
    conn.execute(
        "INSERT INTO observations (ts, ts_server, summary, payload, importance) VALUES (?,?,?,?,1)",
        ("2026-08-30T09:00:00.000Z", "2026-08-30T09:00:01.000Z", "legacy row", "{}"),
    )
    conn.commit()
    conn.close()

    store = RunStore(db_path)  # opens, migrates, backfills
    obs = store.get_observation(1)
    assert obs["local_ts"] is not None
    assert parse_iso(obs["local_ts"]) == datetime(2026, 8, 30, 9, 0, 0, tzinfo=UTC)
    # New writes also get local_ts on the migrated table.
    obs_id = store.add_observation(frame_id=None, ts=iso(), kind="scene", summary="new",
                                   payload={}, importance=1)
    assert store.get_observation(obs_id)["local_ts"] is not None


def test_recent_observations_carry_local_ts(tmp_path):
    run = Run("r", tmp_path, {"model_config": {}})
    run.store.add_observation(frame_id=None, ts=iso(), kind="scene", summary="x",
                              payload={}, importance=1)
    recent = run.store.recent_observations(limit=1)
    assert recent and recent[0]["local_ts"] is not None
    assert utcnow() is not None  # sanity import
