# Privacy and security model

VMA is local-first: nothing leaves the machine unless you explicitly configure
a cloud provider for a stage. Any cloud stage flips the dashboard's egress
warning and is recorded in the run's `metadata.json` (`cloud_used`).

## What is stored, where

- Everything lives under the desktop's data directory (`VMA_DATA_DIR`, default
  `desktop/data/`), one folder per run. There is no cloud sync, no telemetry.
- Each run keeps `observations.db` (SQLite), `media/` (retained JPEGs),
  `reports/`, `exports/`.
- Voice recordings (push-to-talk) are processed in memory by the local STT
  model and are NEVER written to disk.

## Deleting images is NOT deleting sensitive information

Observation **text** persists independently of images. "Person in red jacket
entered the kitchen at 12:04" is stored in the database even after the source
JPEG is deleted by retention, the storage budget, or the "Delete stored
images" button. If you need text gone, delete the run (DELETE
`/api/runs/{run_id}` removes the DB and all media together).

Controls:

| Control | Where | Effect |
|---|---|---|
| Save no images | dashboard → Image storage | observations only, from day one |
| Retention window | `media_retention_minutes` | old images auto-deleted (text stays) |
| Hard byte budget | `media_budget_bytes` | oldest non-important images evicted; important protected while possible |
| Delete stored images | dashboard button / DELETE `/api/runs/{id}/media` | wipes all retained JPEGs of a run; observations stay |
| Delete run | Runs tab / DELETE `/api/runs/{id}` | removes DB + media + reports permanently |
| One-tap pause | dashboard / phone Pause command | stops capture at the source (phone is stop-and-wait) |

## Bystander privacy (documented risk)

A wearable/phone camera run records people who never consented: friends,
roommates, strangers in shops or on the street. Their faces, screens,
documents, and conversations can be captured, described in observation text,
and stored indefinitely. Treat this as your responsibility:

- Only run capture where you have the right to record; prefer locations where
  you can inform people.
- Importance-tagged, importance-filtered storage (`save_frames: important`)
  reduces how many bystander images persist.
- Observation text about third parties is not deleted by image controls — use
  run deletion for that.
- Screen-off / continuous operation amplifies the risk; keep retention and
  budgets tight, and audit `media/` + observation text after runs.

## Continuous camera & voice indicators

- The Android app runs a persistent low-priority foreground notification
  ("VMA Sensor active") whenever the camera is in use; the local preview and
  status chip make capture state visible on the phone.
- The dashboard shows an always-visible system chip (OBSERVING / PAUSED /
  DISCONNECTED / ERROR) that cannot be inferred away — it reflects
  server-side run state, not just connectivity.
- Microphone capture exists ONLY as explicit user action, in two modes:
  - **Push-to-talk** (default): the dashboard's hold-to-talk mic button or
    the Android app's hold-to-talk voice-note bar. The browser shows its own
    recording indicator; the Android bar changes color while recording.
  - **Continuous audio** (phone only, OFF by default, per-device toggle in
    the Sensor tab): while sensing, the phone records ~30s mic segments and
    sends them for local transcription. Android shows its system
    microphone-in-use indicator whenever this runs; the segments are never
    stored on either device. Bystanders in an always-on microphone are a
    substantially bigger risk than push-to-talk — treat the toggle as a
    deliberate, informed decision and keep retention/deletion discipline.
- There is no desktop microphone capture; STT runs on audio sent to it.
- Audio is transcribed in memory and never stored on either device; the
  resulting *transcript text* is stored as a voice-note observation and
  follows the same rules as all observation text (see above: it is sensitive
  even if no audio or image is kept).

## Transport and auth

- Phone ↔ desktop uses WebSocket over your LAN with device-token
  authentication (token stored only as a SHA-256 hash server-side, revocable
  from the dashboard). Traffic is cleartext HTTP/WS on the LAN by default —
  use the relay with `tls://` when crossing the Internet.
- Mobile→desktop commands reuse the same pairing token and are allowlisted
  (`get_status`, `pause`, `resume`, `stop_run`, `append_note`, `chat`); there
  is no shell or arbitrary-execution path in either direction.
- Media paths are resolved strictly inside the run directory (traversal
  rejected); the agent's file tools can write only under `reports/` and
  `exports/`.
- Cloud (OpenAI-compatible) stages receive exactly the bytes/prompt the app
  builds for them — frames for the vision stage, retrieved observation text
  for the reasoning stage. Stored media is not re-inspected by cloud
  providers.
