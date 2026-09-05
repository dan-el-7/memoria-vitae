# Per-run storage schema

Each run lives under `data/runs/<timestamp>_<slug>/` and has its own SQLite
database plus `media/`, `reports/`, and `exports/` directories. The canonical
DDL is [`desktop/vma/store/schema.sql`](../desktop/vma/store/schema.sql).

| Table | Purpose |
|---|---|
| `frames` | Every submitted frame and its sequence, timestamps, GPS, change metrics, verdict, retained media path, and VLM timing. `seq` is unique. |
| `observations` | Structured VLM results and heartbeat/note observations, linked to a frame when applicable. This is the memory: committed observations are never merged or deleted for being semantically similar — temporal history is the point. `kind="voice"` rows are phone/dashboard voice notes: `summary` holds the transcript, `payload.linked_ids` lists temporal + semantic neighbor observation ids (additive links only). `ts`/`ts_server` are ISO-8601 **UTC** (the sort/range key); `local_ts` is the same instant rendered in the run machine's local timezone (with numeric offset) for display — the agent quotes `local_ts` for user-facing times. |
| `observation_fts` | FTS5 index over observation summary and payload detail, maintained by insert/delete triggers. |
| `observation_vec` | One text embedding per **committed** observation (little-endian float32 BLOB, cosine-similarity search in `RunStore.semantic_search`, reciprocal-rank fusion with FTS in `hybrid_search`). Never per-frame. |
| `location_samples` | GPS samples from frames, heartbeats, or device-only updates. |
| `notes` | User and agent notes. |
| `chat_messages` | User, assistant, system, and tool messages plus serialized tool traces. |
| `metrics` | Time-series measurements such as VLM latency, queue depth, and drops. |
| `media` | Retained JPEGs and report assets with byte count and SHA-256. |
| `device_events` | Connect, disconnect, reconnect, gap, and pairing events. |
| `reports` | Generated Markdown reports stored inside the run directory. |

The ingest timestamps (`ts_server`) are authoritative for server-side ordering;
device timestamps are retained for correlation. `frames.verdict` distinguishes
accepted, no-change, stale, and duplicate submissions. `RunStore` handles FTS
search and uses a `LIKE` fallback if FTS5 is unavailable.

Run stats additionally expose `vlm_ms_p50`/`vlm_ms_p95` (inference latency
percentiles), `embeddings` (vectorized observations), and `media_files` /
`media_bytes` (retained-image footprint).

## Image retention controls (config `[pipeline]`)

| Key | Meaning |
|---|---|
| `save_frames` | `none` / `important` (importance ≥ 2) / `all` |
| `media_max_side` | long-side downscale before storage (e.g. 854=480p, 1280=720p, 1920=1080p) |
| `media_jpeg_quality` | storage JPEG quality |
| `media_retention_minutes` | files older than this are deleted (0 = forever); DB rows are cleared, observations stay |
| `media_budget_bytes` | hard cap on retained-image bytes (0 = unlimited); eviction is oldest-first and protects importance ≥ 2 while anything else remains |

## Hour index (`hour_index`, optional HTI fast path)

One compact LLM timeline per CLOSED UTC hour, built by an idle-time worker pass
when `[pipeline] hourly_index = true` (off by default; web dashboard: *Memory
indexing* card). Columns: `hour_start` (unique ISO UTC hour), `summary`
(chronological bullets), `model`, `provider`, `n_obs`, `created_ts`.

Rules: additive only (raw observation rows are never merged, deleted, or
rewritten); the currently open hour is never indexed; the pass is skipped
entirely when the reasoning stage is a cloud provider (egress guard, metric
`hour_index_skipped_cloud`); reindexing an hour REPLACES its digest
(`ON CONFLICT ... DO UPDATE`). The agent reaches it via the
`get_timeline_index` tool.
