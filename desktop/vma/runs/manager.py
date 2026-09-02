"""Run lifecycle: everything belongs to a run.

runs/<ts>_<slug>/
    observations.db   per-run SQLite store
    metadata.json     device, model/provider config, settings snapshot, stats
    media/            retained JPEGs
    reports/          generated markdown reports
    exports/          sandboxed write area for agent file tools

Runs are resumable: reopening a finished run rebuilds its store and chat.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from ..config import AppConfig
from ..store.db import RunStore
from ..utils import iso, read_json, run_slug, tz_offset_string, utcnow, write_json

METADATA_FILE = "metadata.json"
DB_FILE = "observations.db"


class Run:
    def __init__(self, run_id: str, dir_path: Path, meta: dict[str, Any]) -> None:
        self.id = run_id
        self.dir = dir_path
        self.meta = meta
        self.store = RunStore(dir_path / DB_FILE)

    @property
    def reports_dir(self) -> Path:
        return self.dir / "reports"

    @property
    def exports_dir(self) -> Path:
        return self.dir / "exports"

    def media_path(self, rel: str) -> Path | None:
        """Sandboxed resolution of a stored media path (traversal rejected)."""
        base = self.dir.resolve()
        candidate = (base / rel).resolve()
        if not candidate.is_relative_to(base) or not candidate.exists():
            return None
        return candidate

    def save_metadata(self) -> None:
        write_json(self.dir / METADATA_FILE, self.meta)

    def close(self) -> None:
        self.meta["ended_at"] = iso()
        self.meta["stats_final"] = self.store.stats()
        self.save_metadata()
        self.store.close()


class RunManager:
    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self.runs_dir = cfg.runs_dir
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------- create

    def create_run(self, name: str, device: dict[str, Any] | None = None,
                   settings_snapshot: dict[str, Any] | None = None) -> Run:
        now = utcnow()
        run_id = run_slug(name, now)
        run_dir = self.runs_dir / run_id
        (run_dir / "media").mkdir(parents=True, exist_ok=True)
        (run_dir / "reports").mkdir(exist_ok=True)
        (run_dir / "exports").mkdir(exist_ok=True)
        meta: dict[str, Any] = {
            "schema_version": 1,
            "run_id": run_id,
            "name": name,
            "created_at": iso(now),
            "timezone": tz_offset_string(),  # local_ts values below use this offset
            "started_at": None,
            "ended_at": None,
            "device": device or {},
            "model_config": {
                "vision": _provider_meta(self.cfg.vision),
                "reasoning": _provider_meta(self.cfg.reasoning),
            },
            "pipeline_config": {
                "change_mad_threshold": self.cfg.pipeline.change_mad_threshold,
                "change_hash_threshold": self.cfg.pipeline.change_hash_threshold,
                "intake_queue_capacity": self.cfg.pipeline.intake_queue_capacity,
                "min_interval_ms": self.cfg.pipeline.min_interval_ms,
                "max_interval_ms": self.cfg.pipeline.max_interval_ms,
                "save_frames": self.cfg.pipeline.save_frames,
            },
            "cloud_used": {
                "vision": self.cfg.vision.is_cloud(),
                "reasoning": self.cfg.reasoning.is_cloud(),
            },
            "settings_snapshot": settings_snapshot or {},
            "stats_final": None,
        }
        write_json(run_dir / METADATA_FILE, meta)
        run = Run(run_id, run_dir, meta)
        return run

    # -------------------------------------------------------------- list

    def list_runs(self) -> list[dict[str, Any]]:
        out = []
        for d in sorted(self.runs_dir.iterdir(), reverse=True):
            meta_path = d / METADATA_FILE
            db_path = d / DB_FILE
            if not d.is_dir() or not meta_path.exists():
                continue
            try:
                meta = read_json(meta_path)
            except (OSError, ValueError):
                continue
            out.append(
                {
                    "run_id": meta.get("run_id", d.name),
                    "name": meta.get("name", d.name),
                    "created_at": meta.get("created_at"),
                    "ended_at": meta.get("ended_at"),
                    "device": meta.get("device", {}),
                    "cloud_used": meta.get("cloud_used", {}),
                    "has_db": db_path.exists(),
                    "size_bytes": _dir_size(d),
                }
            )
        return out

    def open_run(self, run_id: str) -> Run | None:
        run_dir = self.runs_dir / run_id
        meta_path = run_dir / METADATA_FILE
        if not meta_path.exists():
            return None
        meta = read_json(meta_path)
        return Run(run_id, run_dir, meta)

    def delete_run(self, run_id: str) -> bool:
        """Permanently delete a run directory (observations + media)."""
        run_dir = self.runs_dir / run_id
        if not (run_dir / METADATA_FILE).exists():
            return False
        shutil.rmtree(run_dir)
        return True

    def run_media_path(self, run: Run, rel_path: str) -> Path | None:
        """Resolve a media path stored in the DB, confined to the run dir."""
        base = run.dir.resolve()
        candidate = (base / rel_path).resolve()
        if not candidate.is_relative_to(base) or not candidate.exists():
            return None
        return candidate


def _provider_meta(p: Any) -> dict[str, Any]:
    return {
        "kind": p.kind,
        "model": p.model,
        "base_url": p.base_url if p.is_cloud() else "(local ollama)",
        "num_ctx": p.num_ctx,
        "keep_alive": p.keep_alive,
        "num_gpu": p.num_gpu,
    }


def _dir_size(path: Path) -> int:
    total = 0
    for f in path.rglob("*"):
        if f.is_file():
            total += f.stat().st_size
    return total
