-- Per-run observation database. WAL mode is enabled by the store.
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS frames (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    seq             INTEGER NOT NULL UNIQUE,          -- device-side sequence number
    ts_device       TEXT,
    ts_server       TEXT NOT NULL,                    -- ISO-8601 UTC, ingest time
    path            TEXT,                             -- retained JPEG (may be NULL)
    sha256          TEXT,
    width           INTEGER,
    height          INTEGER,
    gps_lat         REAL,
    gps_lon         REAL,
    gps_accuracy_m  REAL,
    gps_speed_mps   REAL,
    gps_ts          TEXT,
    change_score    REAL,                             -- MAD vs previous accepted frame
    hash_distance   INTEGER,                          -- dHash hamming distance
    verdict         TEXT NOT NULL DEFAULT 'accepted', -- accepted|nochange|stale_dropped|duplicate
    vlm_latency_ms  INTEGER,
    model           TEXT
);
CREATE INDEX IF NOT EXISTS idx_frames_ts ON frames(ts_server);
CREATE INDEX IF NOT EXISTS idx_frames_verdict ON frames(verdict);

CREATE TABLE IF NOT EXISTS observations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    frame_id        INTEGER REFERENCES frames(id),
    ts              TEXT NOT NULL,                    -- observation time, ISO-8601 UTC (sort key)
    ts_server       TEXT NOT NULL,
    local_ts        TEXT,                             -- same instant in machine-local time (display)
    kind            TEXT NOT NULL DEFAULT 'scene',    -- scene|voice|heartbeat|note
    scene           TEXT,
    summary         TEXT NOT NULL,
    importance      INTEGER NOT NULL DEFAULT 1,       -- 0 background .. 3 critical
    importance_reason TEXT,
    confidence      REAL,
    payload         TEXT NOT NULL,                    -- full structured observation JSON
    model           TEXT,
    provider        TEXT,
    latency_ms      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_obs_ts ON observations(ts);
CREATE INDEX IF NOT EXISTS idx_obs_importance ON observations(importance);
CREATE INDEX IF NOT EXISTS idx_obs_frame ON observations(frame_id);

-- Keyword search over observation text (FTS5; falls back handled in code).
CREATE VIRTUAL TABLE IF NOT EXISTS observation_fts USING fts5(
    summary, detail, content='observations', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS obs_ai AFTER INSERT ON observations BEGIN
    INSERT INTO observation_fts(rowid, summary, detail)
    VALUES (new.id, new.summary, substr(new.payload, 1, 4000));
END;
CREATE TRIGGER IF NOT EXISTS obs_ad AFTER DELETE ON observations BEGIN
    INSERT INTO observation_fts(observation_fts, rowid, summary, detail)
    VALUES ('delete', old.id, old.summary, substr(old.payload, 1, 4000));
END;

CREATE TABLE IF NOT EXISTS location_samples (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    lat         REAL NOT NULL,
    lon         REAL NOT NULL,
    accuracy_m  REAL,
    speed_mps   REAL,
    source      TEXT NOT NULL DEFAULT 'heartbeat'  -- frame|heartbeat|device
);
CREATE INDEX IF NOT EXISTS idx_loc_ts ON location_samples(ts);

CREATE TABLE IF NOT EXISTS notes (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    author  TEXT NOT NULL,                            -- user|agent
    text    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    role        TEXT NOT NULL,                        -- user|assistant|tool|system
    content     TEXT NOT NULL,
    tool_trace  TEXT,                                 -- JSON: tool calls + results
    provider    TEXT,
    model       TEXT
);

CREATE TABLE IF NOT EXISTS metrics (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    kind    TEXT NOT NULL,                            -- vlm_latency|queue_depth|drops|fps|...
    value   REAL NOT NULL,
    detail  TEXT
);
CREATE INDEX IF NOT EXISTS idx_metrics_ts ON metrics(ts);

CREATE TABLE IF NOT EXISTS media (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    frame_id  INTEGER REFERENCES frames(id),
    path      TEXT NOT NULL,
    kind      TEXT NOT NULL DEFAULT 'frame',          -- frame|report_asset
    bytes     INTEGER,
    sha256    TEXT
);

CREATE TABLE IF NOT EXISTS device_events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    kind    TEXT NOT NULL,                            -- connect|disconnect|reconnect|gap|pair
    detail  TEXT
);

CREATE TABLE IF NOT EXISTS reports (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    title     TEXT NOT NULL,
    kind      TEXT NOT NULL DEFAULT 'chronological',
    path      TEXT NOT NULL,                          -- markdown inside run dir
    model     TEXT
);

-- Semantic memory: one embedding per COMMITTED observation (never per frame).
-- vec is a little-endian float32 array; similarity is cosine, computed in
-- Python over this BLOB column (SQLite-compatible; a loadable extension such
-- as sqlite-vec can replace the search later without a schema change).
CREATE TABLE IF NOT EXISTS observation_vec (
    obs_id  INTEGER PRIMARY KEY REFERENCES observations(id) ON DELETE CASCADE,
    dim     INTEGER NOT NULL,
    model   TEXT NOT NULL,
    vec     BLOB NOT NULL
);

-- Hierarchical temporal index: one compact LLM timeline per closed hour.
-- Fast path for broad agent queries: read N digests instead of scanning
-- thousands of raw observation rows. Built lazily by the worker when the
-- pipeline toggle is on; raw rows are never deleted or merged (memory pillar).
CREATE TABLE IF NOT EXISTS hour_index (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    hour_start  TEXT NOT NULL UNIQUE,                 -- ISO-8601 UTC hour: 2026-09-06T13:00:00Z
    summary     TEXT NOT NULL,                        -- compact chronological bullets
    model       TEXT,
    provider    TEXT,
    n_obs       INTEGER NOT NULL DEFAULT 0,
    created_ts  TEXT NOT NULL
);

-- Events: contiguous spans of related observations (scene runs separated by a
-- time gap). Derived, additive layer — raw observation rows are never changed.
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    start_ts    TEXT NOT NULL,
    end_ts      TEXT NOT NULL,
    title       TEXT NOT NULL,
    n_obs       INTEGER NOT NULL DEFAULT 0,
    rep_obs_id  INTEGER,                              -- highest-importance observation
    created_ts  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_start ON events(start_ts);
