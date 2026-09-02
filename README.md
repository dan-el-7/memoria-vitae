# VMA — Visual Memory & Analysis Agent

Turn an old Android phone into a **private, continuous visual memory**. The phone
streams ~1 fps camera frames to your desktop over WebSocket; a small local vision
model (VLM) converts each frame into structured observations; everything is stored
in a per-run SQLite database; and a local reasoning LLM lets you **chat with what
you saw** ("where was the calculator at 3pm?", "summarize my afternoon").

Everything runs **local-first** (Ollama by default). No data leaves the machine
unless you explicitly configure a cloud provider for a stage — and the UI tells
you when you did.

```
phone (CameraX, foreground service)
  │  stop-and-wait: ≤1 unacked frame, adaptive rec_interval_ms
  ▼
WebSocket ──► bounded intake (cap 3, latest-frame-wins)
                 │ seq dedup + cheap change gate (64×48 MAD + dHash)
                 ▼
              VLM (schema-constrained JSON, e.g. qwen3-vl:2b)
                 ▼
   per-run SQLite (WAL + FTS5 + embeddings BLOB) + retained media
                 ▼
   hybrid retrieval (FTS + cosine RRF) ──► reasoning LLM + sandboxed tools
```

## Highlights

- **Backpressure, never backlog** — the phone keeps at most one frame in flight;
  the desktop acks *every* frame (duplicates included) and recommends a capture
  interval from measured VLM latency (EMA). Bounded intake with
  latest-frame-wins, so a slow VLM yields fresh frames, not a stale queue.
- **Visual memory, chronological by design** — committed observations are never
  merged or deleted for being similar; similarity only *links* and *ranks*.
  Temporal history is the product.
- **Hybrid retrieval (RAG)** — keyword (FTS5) + semantic (per-observation text
  embeddings via `nomic-embed-text`, cosine in pure Python) fused with
  reciprocal-rank fusion. The agent retrieves only what it needs; images re-enter
  a chat only via an explicit `get_observation_image` / `inspect_frame` tool.
- **Storage you control** — keep no images / important-only / all, downscale
  (480p…original), retention window, and a hard byte budget with eviction that
  protects important frames. Deleting images never deletes observation text
  (documented, deliberate).
- **Voice notes** — push-to-talk on phone or dashboard; local whisper (CPU)
  transcribes; the transcript joins the memory as a linked observation.
  Optional (off by default) continuous audio on the phone.
- **Material 3 Expressive** Android app (Jetpack Compose) with bottom-nav
  menus, always-visible OBSERVING/PAUSED/DISCONNECTED/ERROR state, local-only
  camera preview, and authenticated remote commands (status / pause / resume /
  ask-the-agent / browse any past run).
- **Cross-platform desktop** — Windows/Linux/macOS (pure Python + pathlib;
  platform launchers and env-var config).

## Quickstart

Requirements: Python 3.12+, [Ollama](https://ollama.com), Android Studio (for the app).

```bash
ollama pull qwen3-vl:2b
ollama pull nomic-embed-text          # semantic memory (optional but recommended)

cd desktop
python -m venv .venv
.venv/bin/pip install -e . qrcode psutil uvicorn   # Windows: .venv\Scripts\pip
cd ..

./run_desktop.sh                       # Windows: run_desktop.bat
# dashboard: http://127.0.0.1:8619
```

Pair the phone: generate a code (or scan the dashboard QR) → the Android app
deep-links, pairs, and starts streaming. Voice notes need
`pip install faster-whisper` and `[stt] enabled = true` in `config.toml`
(see `docs/RUNBOOK.md`).

### Android build

```bash
set JAVA_HOME=<Android Studio>\jbr     # or export on Linux/macOS
cd android
gradle assembleDebug                   # offline-friendly; see AGENTS.md notes
# APK: android/app/build/outputs/apk/debug/app-debug.apk
```

The project builds against a project-local Maven mirror created from the
Gradle cache (`.offline-m2`); see `AGENTS.md` for how to reproduce it or just
build online normally.

## Repository layout

| Path | What it is |
|---|---|
| `desktop/` | Python package `vma` — FastAPI app, pipeline, providers, store, agent, STT |
| `relay/` | Standalone asyncio TCP relay for pairing over the Internet (desktop dials out) |
| `android/` | Kotlin + CameraX sensor app (Compose, M3 Expressive) |
| `tests/` | pytest suite (units, sensor-ack regressions, RAG, storage policy, auth) |
| `tools/` | synthetic phone replay + VLM truncation/loop diagnostics |
| `docs/` | architecture, protocol, schema, runbook, providers, privacy, limitations |
| `AGENTS.md` | guidance + verified facts for AI coding agents working on this repo |

## Documentation

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — how the pieces fit
- [`docs/PROTOCOL.md`](docs/PROTOCOL.md) — phone↔desktop WebSocket protocol
- [`docs/SCHEMA.md`](docs/SCHEMA.md) — per-run storage schema
- [`docs/RUNBOOK.md`](docs/RUNBOOK.md) — setup, storage policy, voice, semantic memory
- [`docs/PRIVACY.md`](docs/PRIVACY.md) — what is stored where, bystander risk, deletion model
- [`docs/LIMITATIONS.md`](docs/LIMITATIONS.md) — known limits
- [`AGENTS.md`](AGENTS.md) — verified Ollama/protocol facts for agents

## Privacy

Local-first by architecture: frames, observations, embeddings, and transcripts
never leave the machine unless you opt a stage into a cloud provider (the UI
flags it, and run metadata records it). Camera runs record bystanders who never
consented — read `docs/PRIVACY.md` before running this in public spaces.

## Tests

```bash
cd desktop && .venv/Scripts/python -m pytest ../tests -q    # Windows
cd desktop && .venv/bin/python -m pytest ../tests -q        # Linux/macOS
```

## License

MIT — see [LICENSE](LICENSE).
