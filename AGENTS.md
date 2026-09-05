# AGENTS.md — Visual Memory & Analysis Agent (VMA)

Guidance for AI coding agents working in this repo. Read this first; deeper detail
lives in `docs/` (see the map at the bottom).

## What this project is

An Android phone acts as a remote **visual sensor**: it streams camera frames
(~1 fps, JPEG) over WebSocket to a desktop app. The desktop runs a small vision
LLM (VLM) on each frame to produce structured JSON observations, stores them in a
per-run SQLite database, and later lets the user **chat with the history** through
a reasoning LLM that calls sandboxed retrieval tools. Everything runs local-first
(Ollama by default); cloud providers (OpenAI-compatible) are optional and per-stage.

Design pillars (do not break these without asking):

- **Local-first**: no data leaves the machine unless the user configures a cloud
  provider; any cloud use must flip the UI's egress indicator and be recorded in
  run metadata.
- **Backpressure, never backlog**: the phone is stop-and-wait (≤1 unacked frame);
  the desktop acks EVERY frame (duplicates included — a skipped ack deadlocks the
  phone) with a `rec_interval_ms` recommendation derived from VLM latency EMA.
  Intake queue is bounded (cap 3, latest-frame-wins).
- **8 GB VRAM policy**: VLM pinned (`keep_alive: -1`, numeric — a string `"-1"`
  is rejected by Ollama). Reasoning LLM loads on demand, hybrid placement
  (`num_gpu` ≈ 20) so both models can coexist. **Never unload models because the
  phone disconnected.** Full-GPU placement of the 8B model evicts the VLM (verified).
- **Per-run isolation**: each run is `desktop/data/runs/<ts>_<slug>/` with
  `metadata.json`, `observations.db` (WAL + FTS5 + observation_vec), `media/`,
  `reports/`, `exports/`.
- **Tool sandbox**: agent FS tools resolve paths inside the current run dir;
  writes only under `reports/` and `exports/`; no shell execution.
- **Memory is chronological**: committed observations are NEVER merged or
  deleted for being semantically similar (temporal history is the product).
  Similarity only links/ranks/retrieves. Embeddings run once per *committed*
  observation, never per frame. Images re-enter a chat only via the explicit
  `get_observation_image`/`inspect_frame` tools — never auto-attached.

## Repo layout

| Path | What it is | ( human input, note: this structure was zcode's internal directory and can vary on different setups and os, ask if doubtful or check in case of missing files, repo structure itself will likely line up but the directory it's located in may differ)
|---|---|
| `desktop/` | Python package `vma` — FastAPI app, pipeline, providers, store, agent, security, stt. `.venv` lives here. `config.toml` is auto-saved by the UI at runtime. `data/` (or `VMA_DATA_DIR`) holds `devices.json` + `runs/`. |
| `desktop/vma/pipeline/` | `intake.py` (bounded queue + seq dedup), `change.py` (64×48 MAD + dHash gate), `perceive.py` (schema + prompt), `worker.py` (consumer loop, media policy/budget/retention, embeddings, metrics). |
| `desktop/vma/providers/` | `base.py` protocols (vision/reasoning/embedding), `ollama_provider.py` (+ `OllamaEmbedder`), `openai_compat.py`, `factory.py`. |
| `desktop/vma/stt/` | Modular push-to-talk speech-to-text (`faster_whisper_stt`, lazy import, CPU, optional dep). |
| `desktop/vma/server/sensor.py` | Phone WebSocket endpoint (binary frame framing, acks, run-state push, allowlisted `command` control). |
| `desktop/vma/static/` | Single-page dashboard (vanilla JS, Material-3-ish tokens, always-visible state chip, push-to-talk mic). |
| `relay/` | Standalone asyncio TCP relay (`vma_relay`) for Internet pairing; desktop dials OUT to it. |
| `android/` | Kotlin + CameraX app (classic Views, foreground service, OkHttp WS, offline ring buffer, local preview use case, command buttons). Builds **offline** via `android/.offline-m2` project-local Maven mirror. |
| `tools/synthetic_phone.py` | Replays a folder of images as a paired sensor — use this for desktop-side E2E without a phone. |
| `tests/` | pytest suite (units + sensor-ack regressions + relay integration + expansion regressions). |
| `docs/` | ARCHITECTURE / PROTOCOL / SCHEMA / RUNBOOK / PROVIDERS / ANDROID_PAIRING / PRIVACY / LIMITATIONS / HANDOFF. |

## Commands (Windows cmd.exe — no `;` chaining, no Unix utils)

```bat
:: run the desktop server (dashboard at http://127.0.0.1:8619)
run_desktop.bat          :: from repo root; Linux/macOS: ./run_desktop.sh

:: full test suite (57 tests as of 2026-09-02)
cd desktop && .venv\Scripts\python -m pytest ..\tests -q
:: (root pytest.ini sets asyncio_mode=auto + --basetemp=.tmp; run from anywhere with the venv python)

:: synthetic phone E2E (server must be running; pair with a fresh UI code first)
desktop\.venv\Scripts\python tools\synthetic_phone.py --folder <imgs> --code <PAIRING_CODE>

:: Android APK (offline build; needs JAVA_HOME set to Android Studio's JBR)
set "JAVA_HOME=<Android Studio>\jbr"
cd android
gradle.bat assembleDebug --offline
:: output: android\app\build\outputs\apk\debug\app-debug.apk
```

## Verified facts you must not regress

Ollama (tested on 0.32.x; schema-grammar facts re-verified on 0.33.2):

- **`format` + thinking conflict**: with `think` enabled, `format` (JSON schema)
  is not applied — the model answers in prose. `observe()`/`inspect()` therefore
  hardcode `think: false`; the `enable_thinking` config only gates the reasoning
  chat stage.
- **Never combine `format` + `tools`** in one request (ollama#8095 → empty
  tool_calls). Perception uses `format`; the agent loop uses `tools`.
- `keep_alive` pin/unload values must be numeric (`-1`, `0`), not strings.
- A VLM may return schema JSON in `message.thinking` with empty `content` — the
  provider parses both candidates (`observe()` AND `inspect()`, 0.33.2 behavior).
- **Keep OBSERVATION_SCHEMA fully bounded** (`maxItems` on every array,
  `maxLength` on every string — guarded by `tests/test_schema_bounds.py`):
  with `format`, the schema is a decoding grammar, and an unbounded array/string
  lets a looping VLM generate until `num_ctx` exhaustion cuts the JSON mid-token
  → parse failure (`run 2026-09-01_180711_chennai`: 10/12 frames). 0.33.2
  grammar-enforces `maxItems`/`maxLength`; caps sit above sane output so they
  don't constrain stronger models. Worst case must fit `num_ctx` headroom —
  if the budget test fails, raise `num_ctx`, don't remove bounds.

Protocol (phone ↔ desktop WS):

- Text frames = JSON control (`hello(token)` → `welcome(run_id)`, `ack`, `status`).
- Binary frames = `[u32-LE header length][JSON header][JPEG]`; header carries
  `seq`, `ts_device` (must be UTC `...Z`), optional GPS.
- Server acks every frame: `verdict` ∈ accepted|nochange|stale_dropped|duplicate|error
  plus `rec_interval_ms`. Duplicates are acked AND counted (`frame_duplicate`
  metric → `frames_duplicates` in run stats) so the phone's sent-counter reconciles.
- Dedup is by `(device, seq)` per run; seq must be unique across app restarts
  (Android uses epoch-millis-based values).

Expansion-era verified rules (2026-09-01):

- **Memory pipeline**: commit → embed once (`observation_vec` BLOB, float32 LE,
  cosine in Python) → hybrid retrieval (RRF of FTS + cosine). No sqlite-vec /
  numpy / separate vector service; brute force is fine at per-run scale.
  Default search mode is hybrid (both signals); a dead embedder degrades
  queries to keyword at query time (`embed_query_error` metric) and the
  write-time circuit breaker stops new embeddings — search never errors.
- **Storage policy**: `save_frames` none|important|all + `media_max_side` +
  `media_retention_minutes` + `media_budget_bytes`. Budget eviction is
  oldest-first and protects importance ≥ 2 while anything else remains.
  Retention/budget delete media rows + files but never observation rows.
- **Mobile commands**: one allowlist (`COMMAND_ALLOWLIST` in `app.py`) served
  over `POST /api/command` (device-token auth) and the WS `command` control.
  Unknown commands rejected before any state access; no shell path exists.
- **Voice**: raw-body `POST /api/voice/transcribe` (no python-multipart dep).
  faster-whisper is optional + lazy-imported; STT disabled in config by
  default; push-to-talk only, recordings never touch disk.
- **Android preview**: `Preview` use case renders to the phone screen only and
  never enters the WS/intake path. PreviewView is not used — TextureView +
  `Preview.SurfaceProvider` (works in both the Compose UI and old Views UI).
- **Voice notes join the memory, links never merge**: a phone/dashboard voice
  note is a committed observation (`kind="voice"`, `payload.source` =
  push_to_talk|continuous) embedded once like any other; temporal
  (± `voice_note_context_minutes`) + semantic (cosine ≥ 0.45) neighbors are
  recorded additively in `payload.linked_ids`. Mic capture is hold-to-talk
  by default; the phone's continuous-audio mode (30s segments through the
  sensor FGS with microphone type) is OPT-IN and OFF by default — never
  enable it implicitly. Audio never touches disk on either device.
- Deleting images (retention/budget/wipe endpoint) never deletes observation
  text — text can remain sensitive; run deletion removes both.

Android UI verified rules (Compose Material 3 Expressive, offline):

- The mobile UI is Jetpack Compose on `material3:1.4.0` (M3 Expressive) with
  Kotlin 2.2.10 via AGP 9 built-in Kotlin + `org.jetbrains.kotlin.plugin.compose`.
  Versions are pinned to `.offline-m2` — **do not bump compose/material3/
  activity/lifecycle without mirroring the artifacts first**.
- `.offline-m2` was built from a Gradle cache: KMP artifacts (compose
  `*-android`) sit under variant filenames; `tools/fix_mirror_names.py` adds
  the repo-style `<artifact>-<version>.aar` copies resolution needs.
  `emoji2-views-helper` is forced to 1.3.0 (no mirrored 1.4.0).
- `lifecycle-viewmodel-compose` is NOT mirrored — the Compose UI uses plain
  `mutableStateOf` + the StatusBus poller, no ViewModels.
- `ContainedLoadingIndicator` is absent from material3 1.4.0 stable — use
  `CircularProgressIndicator`. The pre-Compose Views UI is preserved in
  `android/backup_views_ui/` (not built).

- **Times: UTC is the key, local is the voice** (user decision 2026-09-02):
  stored `ts`/`ts_server` stay ISO-8601 UTC (`...Z`) because range queries
  compare ISO strings — never store naive local as the sort key (DST/TZ
  changes break ordering). Every observation row also carries `local_ts`
  (machine-local rendering with numeric offset, backfilled into legacy run
  DBs on open); all display surfaces and the agent's answers quote `local_ts`,
  and the agent system prompt states the machine's UTC offset. Run metadata
  records `timezone`.


VLM schema caps + loop mode (verified 2026-09-02, qwen3-vl:2b / Ollama 0.33.x):

- `format` maxLength is a GUILLOTINE, not a style hint: it hard-clips mid-word.
  A cap set of scene 120 / summary 400 / screen_text 1200 clipped 11/11 scenes,
  6/11 summaries, 5/5 screen_texts on REAL output. Caps must sit ~2x above
  measured natural output; when in doubt measure the run DB first.
- The 2B VLM has an unstable loop mode on screen-dense frames (20k chars of
  numbered repetition until num_ctx cuts the JSON). Two independent levers,
  both now on: grammar caps (contain it) + `repeat_penalty 1.3` on the vision
  stage (prevents it: 4/4 saturated/failed -> 2/4, zero parse failures).
  `repeat_penalty` is config-driven per provider and persisted to config.toml.
- Live verification must check lengths AGAINST caps, not just JSON parsing —
  grammar-truncated output is valid JSON. `tools/check_truncation_live.py`
  and `tools/check_repeat_penalty.py` do this against stored run frames.

## Conventions

- `docs/HANDOFF.md` is the continuation log — **append a dated line for every
  meaningful change** (existing entries follow the pattern).
- The desktop server must be restarted to pick up Python changes; tell the user
  after patching server code.
- Run tests before declaring a desktop-side change done.
- The desktop listens on 127.0.0.1:8619 by default; LAN pairing uses the
  machine's LAN IP shown in the dashboard pairing card.

HTI (hour index, 2026-09-06):

- `hourly_index` is a pipeline toggle (default OFF). The worker indexes closed
  hours only during intake-idle sweeps, max 2 per pass, via
  `HourlyIndexer.build_missing()`.
- Cloud reasoning stages are skipped for indexing (egress guard) — never relax
  this silently.
- `num_gpu` accepts an explicit auto-reset: `null` / `""` / `"auto"` all clear
  the saved layer count back to `None` (Ollama auto-placement). The web UI's
  ↺ button and an emptied gpu-layers field rely on this.
