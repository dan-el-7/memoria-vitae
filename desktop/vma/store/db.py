"""Per-run SQLite store.

Sync sqlite3 (fast enough at ~1 observation/second) executed on the worker
thread; async callers wrap with asyncio.to_thread. FTS5 is used for keyword
search with a LIKE fallback if the runtime lacks it.
"""

from __future__ import annotations

import json
import math
import sqlite3
import struct
from pathlib import Path
from typing import Any

from ..utils import iso, local_iso, parse_iso

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


class RunStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        self._conn.commit()
        self._fts_enabled = self._probe_fts()
        self._migrate_local_ts()

    def _probe_fts(self) -> bool:
        try:
            self._conn.execute("SELECT 1 FROM observation_fts LIMIT 1")
            return True
        except sqlite3.Error:
            return False

    def _migrate_local_ts(self) -> None:
        """Older run DBs lack observations.local_ts; add it and backfill once.

        UTC ts stays the sort key; local_ts is a display convenience rendered
        in whatever the machine's timezone was at open time.
        """
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(observations)")}
        if "local_ts" not in cols:
            self._conn.execute("ALTER TABLE observations ADD COLUMN local_ts TEXT")
            self._conn.commit()
        nulls = self._conn.execute(
            "SELECT COUNT(*) FROM observations WHERE local_ts IS NULL"
        ).fetchone()[0]
        if not nulls:
            return
        rows = self._conn.execute(
            "SELECT id, ts FROM observations WHERE local_ts IS NULL LIMIT 20000"
        ).fetchall()
        updates = []
        for r in rows:
            try:
                updates.append((local_iso(parse_iso(r["ts"])), r["id"]))
            except (ValueError, TypeError):
                continue
        self._conn.executemany(
            "UPDATE observations SET local_ts=? WHERE id=?", updates
        )
        self._conn.commit()

    def close(self) -> None:
        try:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        self._conn.close()

    # ------------------------------------------------------------- frames

    def add_frame(self, *, seq: int, ts_server: str, ts_device: str | None, width: int, height: int,
                  gps: dict[str, Any] | None, change_score: float | None, hash_distance: int | None,
                  verdict: str) -> int | None:
        """Insert a frame row. Returns None if seq is a duplicate (replay after reconnect)."""
        try:
            cur = self._conn.execute(
                """INSERT INTO frames (seq, ts_device, ts_server, width, height,
                     gps_lat, gps_lon, gps_accuracy_m, gps_speed_mps, gps_ts,
                     change_score, hash_distance, verdict)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    seq, ts_device, ts_server, width, height,
                    _g(gps, "lat"), _g(gps, "lon"), _g(gps, "accuracy_m"), _g(gps, "speed_mps"),
                    _g(gps, "ts"), change_score, hash_distance, verdict,
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid)
        except sqlite3.IntegrityError:
            return None  # duplicate seq -> reconnection replay, already stored

    def set_frame_media(self, frame_id: int, path: str, sha256: str, size: int) -> None:
        self._conn.execute(
            "UPDATE frames SET path=?, sha256=? WHERE id=?", (path, sha256, frame_id)
        )
        self._conn.execute(
            "INSERT INTO media (frame_id, path, kind, bytes, sha256) VALUES (?,?,?,?,?)",
            (frame_id, path, "frame", size, sha256),
        )
        self._conn.commit()

    def set_frame_verdict(self, frame_id: int, verdict: str,
                          vlm_latency_ms: int | None = None, model: str | None = None) -> None:
        self._conn.execute(
            "UPDATE frames SET verdict=?, vlm_latency_ms=?, model=? WHERE id=?",
            (verdict, vlm_latency_ms, model, frame_id),
        )
        self._conn.commit()

    def frame_by_seq(self, seq: int) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM frames WHERE seq=?", (seq,)).fetchone()
        return dict(row) if row else None

    def frame_by_id(self, frame_id: int) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM frames WHERE id=?", (frame_id,)).fetchone()
        return dict(row) if row else None

    def latest_accepted_frame(self) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM frames WHERE verdict IN ('accepted') ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------- media storage

    def media_bytes_total(self) -> int:
        val = self._conn.execute("SELECT COALESCE(SUM(bytes), 0) FROM media").fetchone()[0]
        return int(val or 0)

    def media_rows_for_eviction(self, limit: int = 50) -> list[dict[str, Any]]:
        """Oldest-first media rows; important (>=2) observations sort last.

        Eviction walks this list and stops as soon as the budget is met, so
        important images are protected while anything else remains.
        """
        rows = self._conn.execute(
            """SELECT m.id AS media_id, m.path, m.frame_id, m.bytes,
                      f.ts_server, COALESCE(o.importance, 0) AS importance
               FROM media m
               LEFT JOIN frames f ON f.id = m.frame_id
               LEFT JOIN observations o ON o.frame_id = f.id
               ORDER BY (CASE WHEN COALESCE(o.importance, 0) >= 2 THEN 1 ELSE 0 END),
                        f.ts_server ASC, m.id ASC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_media_row(self, media_id: int) -> str | None:
        """Remove one media row + its frames.path reference. Returns the rel path."""
        row = self._conn.execute("SELECT path, frame_id FROM media WHERE id=?", (media_id,)).fetchone()
        if row is None:
            return None
        self._conn.execute("DELETE FROM media WHERE id=?", (media_id,))
        if row["frame_id"] is not None:
            self._conn.execute(
                "UPDATE frames SET path=NULL, sha256=NULL WHERE id=?", (row["frame_id"],)
            )
        self._conn.commit()
        return row["path"]

    def delete_all_media_rows(self) -> int:
        """Privacy control: wipe every retained image; observations stay."""
        paths = [r["path"] for r in self._conn.execute("SELECT path FROM media").fetchall()]
        self._conn.execute("DELETE FROM media")
        self._conn.execute("UPDATE frames SET path=NULL, sha256=NULL")
        self._conn.commit()
        return len(paths)

    def old_media_rows(self, older_than_iso: str, limit: int = 200) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """SELECT m.id AS media_id, m.path FROM media m
               LEFT JOIN frames f ON f.id = m.frame_id
               WHERE COALESCE(f.ts_server, '') < ?
               ORDER BY f.ts_server ASC LIMIT ?""",
            (older_than_iso, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------- observations

    def add_observation(self, *, frame_id: int | None, ts: str, kind: str, summary: str,
                        payload: dict[str, Any], importance: int = 1, confidence: float | None = None,
                        scene: str | None = None, importance_reason: str | None = None,
                        model: str | None = None, provider: str | None = None,
                        latency_ms: int | None = None) -> int:
        try:
            local_ts = local_iso(parse_iso(ts))
        except (ValueError, TypeError):
            local_ts = None
        cur = self._conn.execute(
            """INSERT INTO observations (frame_id, ts, ts_server, local_ts, kind, scene, summary, importance,
                   importance_reason, confidence, payload, model, provider, latency_ms)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (frame_id, ts, iso(), local_ts, kind, scene, summary, importance, importance_reason,
             confidence, json.dumps(payload, ensure_ascii=False), model, provider, latency_ms),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def get_observation(self, obs_id: int) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM observations WHERE id=?", (obs_id,)).fetchone()
        return _obs(row)

    def observations_in_range(self, start: str, end: str, *, limit: int = 500) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM observations WHERE ts >= ? AND ts <= ? ORDER BY ts LIMIT ?",
            (start, end, limit),
        ).fetchall()
        return [_obs(r) for r in rows]

    def search_observations(self, query: str, *, start: str | None = None, end: str | None = None,
                            importance_min: int = 0, limit: int = 30) -> list[dict[str, Any]]:
        """FTS keyword search with time/importance filters and LIKE fallback."""
        if self._fts_enabled and query.strip():
            try:
                rows = self._conn.execute(
                    """SELECT o.* FROM observation_fts f
                       JOIN observations o ON o.id = f.rowid
                       WHERE observation_fts MATCH ? AND o.importance >= ?
                       ORDER BY rank LIMIT ?""",
                    (query, importance_min, limit),
                ).fetchall()
                results = [_obs(r) for r in rows]
            except sqlite3.Error:
                results = []
            if results:
                return self._maybe_filter_time(results, start, end)
        like = f"%{query}%"
        rows = self._conn.execute(
            """SELECT * FROM observations
               WHERE importance >= ? AND (summary LIKE ? OR payload LIKE ?)
               ORDER BY ts DESC LIMIT ?""",
            (importance_min, like, like, limit * 2),
        ).fetchall()
        return self._maybe_filter_time([_obs(r) for r in rows], start, end)[:limit]

    @staticmethod
    def _maybe_filter_time(rows: list[dict[str, Any]], start: str | None, end: str | None) -> list[dict[str, Any]]:
        if not start and not end:
            return rows
        return [r for r in rows if (not start or r["ts"] >= start) and (not end or r["ts"] <= end)]

    def recent_observations(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM observations ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_obs(r) for r in reversed(rows)]

    # ---------------------------------------------------------- embeddings

    def set_observation_embedding(self, obs_id: int, dim: int, model: str, vec: bytes) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO observation_vec (obs_id, dim, model, vec) VALUES (?,?,?,?)",
            (obs_id, dim, model, vec),
        )
        self._conn.commit()

    def embedding_count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM observation_vec").fetchone()[0])

    def all_embeddings(self) -> list[tuple[int, bytes]]:
        """(obs_id, vec_bytes) for every embedded observation."""
        return [
            (int(r[0]), r[1])
            for r in self._conn.execute("SELECT obs_id, vec FROM observation_vec").fetchall()
        ]

    def semantic_search(self, query_vec: list[float], *, importance_min: int = 0,
                        limit: int = 30, start: str | None = None,
                        end: str | None = None) -> list[dict[str, Any]]:
        """Cosine-similarity ranking over embedded observations.

        Brute force on purpose: per-run corpora are small (≤ a few per second
        of wall time), vectors live in the same per-run SQLite file, and no
        extension or extra service is required. Attach observation rows for
        ids above the score threshold order.
        """
        where = ["o.importance >= ?"]
        params: list[Any] = [importance_min]
        if start:
            where.append("o.ts >= ?")
            params.append(start)
        if end:
            where.append("o.ts <= ?")
            params.append(end)
        rows = self._conn.execute(
            f"""SELECT o.*, v.vec FROM observation_vec v
               JOIN observations o ON o.id = v.obs_id
               WHERE {" AND ".join(where)}""",
            params,
        ).fetchall()
        q = [float(x) for x in query_vec]
        qnorm = math.sqrt(sum(x * x for x in q)) or 1.0
        scored: list[tuple[float, sqlite3.Row]] = []
        for row in rows:
            vec = _vec_to_floats(row["vec"])
            if len(vec) != len(q):
                continue  # dimension change (model swap): skip stale vectors
            num = 0.0
            for a, b in zip(q, vec):
                num += a * b
            vnorm = math.sqrt(sum(b * b for b in vec)) or 1.0
            scored.append((num / (qnorm * vnorm), row))
        scored.sort(key=lambda t: t[0], reverse=True)
        out = []
        for score, row in scored[:limit]:
            obs = _obs(row)
            if obs is None:
                continue
            obs["similarity"] = round(score, 4)
            out.append(obs)
        return out

    def hybrid_search(self, query_vec: list[float] | None, query: str, *,
                      importance_min: int = 0, limit: int = 30,
                      start: str | None = None, end: str | None = None) -> list[dict[str, Any]]:
        """Reciprocal-rank fusion of keyword (FTS/LIKE) and semantic results."""
        keyword = self.search_observations(query, start=start, end=end,
                                           importance_min=importance_min, limit=limit)
        semantic = self.semantic_search(query_vec, importance_min=importance_min,
                                        limit=limit, start=start, end=end) if query_vec else []
        if not semantic:
            return keyword
        if not keyword:
            return semantic
        k = 60.0
        scores: dict[int, float] = {}
        rows: dict[int, dict[str, Any]] = {}
        for rank, obs in enumerate(keyword):
            scores[obs["id"]] = scores.get(obs["id"], 0.0) + 1.0 / (k + rank + 1)
            rows[obs["id"]] = obs
        for rank, obs in enumerate(semantic):
            scores[obs["id"]] = scores.get(obs["id"], 0.0) + 1.0 / (k + rank + 1)
            row = rows.setdefault(obs["id"], obs)
            row.setdefault("similarity", obs.get("similarity"))
        fused = sorted(rows.values(), key=lambda o: scores[o["id"]], reverse=True)
        return fused[:limit]

    # ------------------------------------------------------------ location

    def add_location(self, ts: str, lat: float, lon: float, accuracy_m: float | None,
                     speed_mps: float | None, source: str) -> None:
        self._conn.execute(
            "INSERT INTO location_samples (ts, lat, lon, accuracy_m, speed_mps, source) VALUES (?,?,?,?,?,?)",
            (ts, lat, lon, accuracy_m, speed_mps, source),
        )
        self._conn.commit()

    def location_history(self, start: str | None = None, end: str | None = None,
                         limit: int = 1000) -> list[dict[str, Any]]:
        if start and end:
            rows = self._conn.execute(
                "SELECT * FROM location_samples WHERE ts>=? AND ts<=? ORDER BY ts LIMIT ?",
                (start, end, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM location_samples ORDER BY ts LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- misc

    def add_note(self, text: str, author: str = "agent") -> int:
        cur = self._conn.execute(
            "INSERT INTO notes (ts, author, text) VALUES (?,?,?)", (iso(), author, text)
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def add_chat_message(self, role: str, content: str, *, tool_trace: dict[str, Any] | None = None,
                         provider: str | None = None, model: str | None = None) -> int:
        cur = self._conn.execute(
            """INSERT INTO chat_messages (ts, role, content, tool_trace, provider, model)
               VALUES (?,?,?,?,?,?)""",
            (iso(), role, content, json.dumps(tool_trace, ensure_ascii=False) if tool_trace else None,
             provider, model),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def chat_history(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM chat_messages ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def add_metric(self, kind: str, value: float, detail: dict[str, Any] | None = None) -> None:
        self._conn.execute(
            "INSERT INTO metrics (ts, kind, value, detail) VALUES (?,?,?,?)",
            (iso(), kind, value, json.dumps(detail) if detail else None),
        )
        self._conn.commit()

    def add_device_event(self, kind: str, detail: dict[str, Any] | None = None) -> None:
        self._conn.execute(
            "INSERT INTO device_events (ts, kind, detail) VALUES (?,?,?)",
            (iso(), kind, json.dumps(detail) if detail else None),
        )
        self._conn.commit()

    def add_report(self, title: str, kind: str, path: str, model: str | None = None) -> int:
        cur = self._conn.execute(
            "INSERT INTO reports (ts, title, kind, path, model) VALUES (?,?,?,?,?)",
            (iso(), title, kind, path, model),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def list_reports(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM reports ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict[str, Any]:
        def one(sql: str, *args: Any) -> Any:
            return self._conn.execute(sql, args).fetchone()[0]

        first = one("SELECT MIN(ts) FROM observations")
        last = one("SELECT MAX(ts) FROM observations")
        latencies = sorted(
            r[0] for r in self._conn.execute(
                "SELECT vlm_latency_ms FROM frames WHERE vlm_latency_ms IS NOT NULL"
            ).fetchall()
        )
        p50 = p95 = None
        if latencies:
            p50 = latencies[min(len(latencies) - 1, int(round(0.50 * (len(latencies) - 1))))]
            p95 = latencies[min(len(latencies) - 1, int(round(0.95 * (len(latencies) - 1))))]
        return {
            "frames_total": one("SELECT COUNT(*) FROM frames"),
            "frames_accepted": one("SELECT COUNT(*) FROM frames WHERE verdict='accepted'"),
            "frames_dropped_stale": one("SELECT COUNT(*) FROM frames WHERE verdict='stale_dropped'"),
            "frames_nochange": one("SELECT COUNT(*) FROM frames WHERE verdict='nochange'"),
            "frames_error": one("SELECT COUNT(*) FROM frames WHERE verdict='error'"),
            "frames_duplicates": one("SELECT COUNT(*) FROM metrics WHERE kind='frame_duplicate'"),
            "avg_vlm_ms": one(
                "SELECT AVG(vlm_latency_ms) FROM frames WHERE vlm_latency_ms IS NOT NULL"
            ),
            "vlm_ms_p50": p50,
            "vlm_ms_p95": p95,
            "observations": one("SELECT COUNT(*) FROM observations"),
            "important_observations": one("SELECT COUNT(*) FROM observations WHERE importance>=2"),
            "issues": one(
                "SELECT COUNT(*) FROM observations WHERE payload LIKE '%\"issues\": [{%'"
            ),
            "embeddings": self.embedding_count(),
            "media_files": one("SELECT COUNT(*) FROM media"),
            "media_bytes": self.media_bytes_total(),
            "first_ts": first,
            "last_ts": last,
            "notes": one("SELECT COUNT(*) FROM notes"),
            "reports": one("SELECT COUNT(*) FROM reports"),
        }

    # ------------------------------------------------- hour index (HTI)

    def unindexed_closed_hours(self, before_hour: str, limit: int = 2) -> list[tuple[str, int]]:
        """Oldest closed UTC hours with observations but no hour_index row yet.

        `before_hour` is the current hour prefix ('YYYY-MM-DDTHH') — the open
        hour is never indexed so a summary can't miss later observations.
        """
        rows = self._conn.execute(
            """SELECT substr(o.ts, 1, 13) AS h, COUNT(*) AS n
               FROM observations o
               WHERE substr(o.ts, 1, 13) != ?
               GROUP BY h
               HAVING h NOT IN (SELECT substr(hour_start, 1, 13) FROM hour_index)
               ORDER BY h
               LIMIT ?""",
            (before_hour, limit),
        ).fetchall()
        return [(r["h"], r["n"]) for r in rows]

    def observations_for_hour(self, hour_prefix: str, limit: int = 240) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """SELECT ts, kind, importance, scene, summary FROM observations
               WHERE substr(ts, 1, 13) = ? ORDER BY ts LIMIT ?""",
            (hour_prefix, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def add_hour_index(self, *, hour_start: str, summary: str, model: str | None,
                       provider: str | None, n_obs: int) -> None:
        self._conn.execute(
            """INSERT INTO hour_index (hour_start, summary, model, provider, n_obs, created_ts)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(hour_start) DO UPDATE SET summary=excluded.summary,
                   model=excluded.model, provider=excluded.provider,
                   n_obs=excluded.n_obs, created_ts=excluded.created_ts""",
            (hour_start, summary[:4000], model, provider, n_obs, iso()),
        )
        self._conn.commit()

    def hours_in_range(self, start: str | None = None, end: str | None = None,
                       limit: int = 200) -> list[dict[str, Any]]:
        sql = "SELECT hour_start, summary, n_obs, model, created_ts FROM hour_index"
        conds, args = [], []
        if start:
            conds.append("hour_start >= ?")
            args.append(start)
        if end:
            conds.append("hour_start <= ?")
            args.append(end)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY hour_start LIMIT ?"
        args.append(limit)
        rows = self._conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def hour_index_count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM hour_index").fetchone()[0])

    # ------------------------------------------------------------- delete

    def delete_all(self) -> None:
        """Wipe run contents (used when a run is deleted)."""
        for table in ("observation_fts", "observation_vec", "observations", "media", "frames",
                      "location_samples", "notes", "chat_messages", "metrics", "device_events",
                      "reports", "hour_index"):
            if table == "observation_fts":
                try:
                    self._conn.execute("DELETE FROM observation_fts")
                except sqlite3.Error:
                    pass
            else:
                self._conn.execute(f"DELETE FROM {table}")
        self._conn.commit()


def _g(d: dict[str, Any] | None, key: str) -> Any:
    if not d:
        return None
    return d.get(key)


def vec_to_bytes(vec: list[float]) -> bytes:
    """float32 little-endian BLOB for the observation_vec table."""
    return struct.pack(f"<{len(vec)}f", *[float(x) for x in vec])


def _vec_to_floats(blob: bytes | memoryview) -> list[float]:
    count = len(blob) // 4
    return list(struct.unpack(f"<{count}f", bytes(blob[: count * 4])))


def _obs(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    out.pop("vec", None)
    try:
        out["payload"] = json.loads(out["payload"])
    except (json.JSONDecodeError, TypeError):
        pass
    return out
