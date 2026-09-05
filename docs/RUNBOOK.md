# VMA runbook

## Desktop

Windows: double-click `run_desktop.bat` (repo root), or:

```bat
cd desktop
.venv\Scripts\python -m uvicorn vma.app:app --host 0.0.0.0 --port 8619
```

Linux/macOS: `./run_desktop.sh` (create the venv once:
`python3 -m venv desktop/.venv && desktop/.venv/bin/pip install -e desktop qrcode psutil uvicorn`).
Environment overrides: `VMA_HOST`, `VMA_PORT`, `VMA_DATA_DIR` (base dir for
`devices.json` and `runs/`). The Python core is platform-independent; only
launcher scripts and docs mention Windows paths.

Open `http://localhost:8619`. Generate a pairing code in the dashboard, pair
the Android app (or scan the QR with the phone's camera), start a run, and
leave the dashboard open to watch pipeline and model status. The always-visible
chip in the header is the authoritative system state: OBSERVING / PAUSED /
DISCONNECTED / ERROR.

The default VLM is `qwen3-vl:2b`; pull it before the first run if it is not
already installed:

```text
ollama pull qwen3-vl:2b
ollama pull nomic-embed-text   :: semantic memory (embeddings stage)
```

The default desktop bind is `127.0.0.1`; use `--host 0.0.0.0` for a trusted LAN
and restrict access with the host firewall.

## Image storage policy

Configure in the dashboard (Image storage card) or `config.toml` `[pipeline]`:
`save_frames` (none/important/all), `media_max_side` (original/1080p/720p/480p
or any number), `media_retention_minutes`, and `media_budget_bytes`. The budget
evicts oldest non-important images first; importance ≥ 2 images are protected
while anything else remains. Observations (text) are never deleted by these
controls — see `docs/PRIVACY.md` for why deleting images is not deleting
sensitive information.

## Voice / speech-to-text (push-to-talk)

Local CPU transcription via faster-whisper (optional dependency):

```bat
cd desktop
.venv\Scripts\pip install faster-whisper
```

Then enable it in `config.toml` (`[stt] enabled = true`; default model
`small`, int8, CPU — matches the project decision). Restart the desktop.

Voice capture has two modes — manual push-to-talk, and an opt-in continuous
mode (OFF by default); audio is never written to disk in either:

- **Web dashboard** (Chat tab): hold the 🎙 button to transcribe into the
  chat input; tick "also save as voice note" to commit it to the run.
- **Android app** (Sensor tab): hold the voice-note bar to record and send
  with the device token (`POST /api/voice/note`). Requires `RECORD_AUDIO`,
  requested on first use, activity-scoped only — no background microphone.

Continuous listening does exist as an explicit opt-in (it is OFF by default
by design): toggle **Continuous audio** in the app's Sensor tab and, while
sensing, the phone records ~30s mic segments and sends each to
`/api/voice/note?source=continuous`. The desktop transcribes them with the
same whisper pipeline (silent segments produce nothing) and commits them as
linked voice observations. Battery use increases; Android's system
microphone indicator shows while it runs.

A voice note becomes an ordinary committed observation (`kind="voice"`):
embedded once like any other observation, searchable (FTS + semantic), and
**linked** (never merged) to nearby observations — temporal neighbors within
`voice_note_context_minutes` (default 2) plus semantic neighbors
(cosine ≥ 0.45). Links are stored in `payload.linked_ids` and count as
"managed" history; nothing is deleted or deduplicated by linking.

## Semantic memory (RAG)

Each committed observation is embedded once with `nomic-embed-text` (local
Ollama). The agent's `search_observations` tool defaults to hybrid retrieval
(FTS keywords fused with cosine-similarity ranking); use `mode: "semantic"`
for paraphrase-style questions. Embeddings are disabled automatically (circuit
breaker, 3 strikes) if the model is missing — keyword search keeps working.
`get_observation_image` is the ONLY way images enter a chat: they are attached
when the agent explicitly asks, never automatically.

## Android

The verified debug artifact is:

`android/app/build/outputs/apk/debug/app-debug.apk`

Build it with Android Studio JBR 21 and the cached Gradle 9.4.1 distribution:

```bat
set JAVA_HOME=<Android Studio>\jbr
cd android
gradle.bat assembleDebug
```

Install with Android Studio or `adb install -r` from a machine with an attached
device. The first device test should verify camera permission, foreground
service notification, GPS permission, reconnect, and the bounded offline buffer.

## Synthetic replay and tests

```bat
cd desktop
.venv\Scripts\python -m pytest ..\tests -q
python ..\tools\synthetic_phone.py --folder path\to\images --host 127.0.0.1:8619 --code PAIRING_CODE
```

The replay waits for the matching frame acknowledgement and honors the server's
recommended interval. Start a run before sending images.

## Relay

From `relay/`:

```text
python -m vma_relay.server --host 0.0.0.0 --port 8765 --reg-token change-me
```

Configure the desktop with `server.relay_url = "tcp://relay-host:8765"` and
the same value in `server.relay_reg_token`. Use `tls://` with a trusted
certificate for direct TLS. The included Dockerfile exposes port 8765.

For an Internet-facing relay, use a strong registration secret, TLS, firewall
rules, and an attach credential in front of the current development channel-id
flow. Do not treat the opaque channel id as a secret.

## Useful checks

- `GET /api/health` confirms the FastAPI process is alive.
- `GET /api/status` reports run, phone, relay, queue, and model state.
- A phone with no active run should receive `welcome` with `run_id: null`.
- A frame is not evidence of an observation until its ack and the run stats show
  it was accepted or classified as no-change.

## Hierarchical Temporal Indexing (HTI)

Toggle: `[pipeline] hourly_index = true` in `config.toml`, or the *Memory
indexing* card on the dashboard. When enabled, the pipeline worker compresses
each finished hour of observations into a short LLM timeline (`hour_index`
table) while the intake queue is idle — indexing never competes with live
perception. Broad agent questions ("summarize my afternoon") hit the
`get_timeline_index` fast path first; drill-down stays on the row tools.

Notes: local reasoning models only (cloud reasoning stages are skipped — the
digest would ship observation text off-machine); backfill is progressive,
oldest hours first; the open hour is indexed only after it closes; raw
observations are never altered.

## Mark this moment (phone)

The Sensor tab has a **Mark this moment** button. It sends the allowlisted
`mark_moment` command (`window_seconds`, default 60): the desktop raises the
importance of everything committed in that window to 3 (retrieval
`importance_min` filters pick it up, media eviction protects the frames) and
appends a user note. Say something with hold-to-talk right before/after to
attach context.

## Cross-run search

The agent's `search_all_runs` tool keyword-searches every run's DB on the
machine (read-only, most recent 20 runs) so questions like "which day was I in
the garage" work across sessions. Drill-down stays per-run.

## Encryption at rest

Toggle: dashboard **Encryption at rest** card or `POST /api/security/encryption`.
When enabled, a Fernet key is generated once at `data/secret.key` and applied
transparently to new writes: the observation `payload` column (screen text,
transcripts) is stored as `enc1:` ciphertext and retained media files get a
`VMAENC1:` prefix. Consequences: keyword search then covers summary + scene
only (payload ciphertext is not FTS-indexed); legacy plaintext rows remain
readable; disabling stops encrypting new writes but never decrypts existing
data. **Do not delete `secret.key` while encrypted rows exist** — they become
permanently unreadable (tests: `test_v022_features.py`).
