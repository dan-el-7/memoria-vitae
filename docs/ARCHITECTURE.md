# VMA architecture

Visual Memory & Analysis Agent (VMA) treats the Android phone as a remote visual
sensor. The desktop owns the run, inference, storage, pairing, and operator UI.

```text
Android CameraX + GPS
        │ WebSocket (JSON controls + binary JPEG frames)
        ▼
FastAPI SensorHub ──► bounded FrameIntake ──► PipelineWorker
        │                                      │
        │                                      ├─ MAD + dHash change gate
        │                                      ├─ vision provider
        │                                      └─ per-run SQLite + media
        │
        ├─ dashboard REST/WebSocket UI
        ├─ pairing manager (single-use code, hashed device tokens)
        └─ agent chat ──► search/notes/reports/inspection tools
```

## Desktop boundaries

- `vma.server.sensor.SensorHub` accepts one active phone session and authenticates
  the device token before frames are admitted.
- `vma.pipeline.intake.FrameIntake` has a bounded capacity of three. When full,
  the oldest queued frame is evicted; sequence numbers are deduplicated.
- `vma.pipeline.worker.PipelineWorker` performs the change gate, VLM call,
  persistence, media policy, and adaptive interval calculation. A failed VLM
  call does not terminate the worker.
- `vma.store.db.RunStore` owns one SQLite database per run. Run directories also
  contain `media/`, `reports/`, and `exports/`.
- Providers are selected independently for vision and reasoning. Ollama is the
  local default; the OpenAI-compatible provider is opt-in and is marked in run
  metadata when used.
- The dashboard is deliberately no-build HTML/CSS/JavaScript. This keeps the
  desktop runnable without Node while exposing run controls, observations,
  model state, pairing, and chat.

## Relay boundary

The relay is a separate asyncio TCP process. It never parses phone frames. A
desktop opens an outbound connection and registers a channel; a phone attaches
to that channel. The desktop adapts the resulting framed stream to the same
`SensorHub` interface used by LAN WebSockets. The phone's normal device token is
still checked by the desktop after the tunnel is established.

The current relay provides optional direct TLS and desktop registration-token
authentication. It is suitable for development and a controlled deployment;
production Internet exposure still needs a deployment policy, attach secret or
short-lived pairing credential, rate limits, metrics, and end-to-end encryption.
