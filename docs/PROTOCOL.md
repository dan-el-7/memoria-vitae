# VMA transport protocol

## Phone ↔ desktop WebSocket

Endpoint: `GET /ws/phone`.

Control messages are UTF-8 JSON text frames. Image uploads are binary frames:

```text
[4-byte unsigned little-endian header length]
[UTF-8 JSON header]
[JPEG bytes]
```

The header is:

```json
{
  "seq": 42,
  "ts_device": "2026-08-30T12:34:56.789Z",
  "w": 1080,
  "h": 810,
  "gps": {
    "lat": 12.9,
    "lon": 80.2,
    "accuracy_m": 10.0,
    "speed_mps": 0.0,
    "ts": "2026-08-30T12:34:56.789Z"
  }
}
```

`gps` may be null or omitted. Device timestamps must be UTC ISO-8601 strings
ending in `Z`; the desktop supplies authoritative sequence, receipt, and run
metadata to the stored observation.

The first control message is:

```json
{"type":"hello","token":"DEVICE_TOKEN","device":{"model":"…","app_version":"…"}}
```

After successful authentication the desktop sends `welcome` with `run_id`,
`min_interval_ms`, and `heartbeat_s`. If `run_id` is null, the phone should
remain connected for status/heartbeat but must not upload frames.

Each submitted frame receives an `ack` containing `seq`, `verdict`,
`rec_interval_ms`, and `queue`. The phone uses stop-and-wait: it keeps at most
one frame unacknowledged and waits for the recommended interval before the next
capture. Valid verdicts include `accepted`, `nochange`, `stale_dropped`,
`duplicate`, and `dropped_no_run`.

Other controls are `heartbeat` (optional GPS), `ping`/`pong`, `status`,
`command` (see below), and `error`. Reconnection uses capped exponential
backoff with jitter. The Android buffer is an in-memory ring and is
intentionally bounded; the server's sequence deduplication makes replay after
reconnect safe.

## Mobile → desktop commands

An authenticated phone may drive the desktop over the same socket (or via
`POST /api/command` with `{token, command, args}` — same auth, no second
system). Control message:

```json
{"type":"command","command":"pause","args":{}}
```

The desktop replies with `{"type":"command_result","ok":true,"command":…,"result":…}`
or `{"type":"command_result","ok":false,…,"error":…}`. The allowlist is fixed
server-side (`COMMAND_ALLOWLIST` in `app.py`): `get_status`, `pause`,
`resume`, `stop_run`, `append_note`, `chat`. Anything else is rejected; there
is no shell or arbitrary desktop execution path.

## Push-to-talk transcription

`POST /api/voice/transcribe` with the audio recording as the raw request body
and optional `?send_to_agent=true`. Returns
`{"transcription": {text, language, duration_s, segments, model, elapsed_ms}}`
(and `agent_answer` when `send_to_agent` is used). Requires `[stt] enabled=true`
in config plus the optional `faster-whisper` package; otherwise HTTP 503.
Continuous listening does not exist by design — recordings are memory-only.

## Voice notes → observation memory

`POST /api/voice/note?source=push_to_talk|continuous` with the audio as the
raw request body and an optional
`X-VMA-Token` header (a paired device's token; required when present,
consistent with the token-authed command channel). The desktop transcribes
locally and commits the transcript as an observation (`kind="voice"`,
importance 2, `payload.source` records the mode) linked to temporal
neighbors (± `voice_note_context_minutes`) and semantic neighbors
(cosine ≥ 0.45) via `payload.linked_ids` — links are additive; nothing is
merged or deleted. Responses: `{"saved": true, "observation_id": …, "links": […], …}`,
`{"saved": false, "reason": "nothing recognized"}`, or HTTP 401/409/503.
Audio is processed in memory only and never stored. `source=continuous`
segments come from the phone's opt-in continuous-audio mode.

## Desktop ↔ relay

The first packet is a newline-terminated JSON handshake:

```json
{"role":"desktop","channel_id":"desk_…","token":"RELAY_REG_TOKEN"}
{"role":"phone","channel_id":"desk_…"}
```

The relay responds with `registered` or `attached`. Once accepted, each packet
is:

```text
[4-byte unsigned big-endian envelope length]
[1-byte kind: J=json, B=binary, E=relay event, C=close]
[opaque payload]
```

Relay events are JSON payloads such as `phone_attached`, `phone_detached`, and
`desktop_detached`. The relay forwards `J` and `B` payloads unchanged; the
desktop then treats them as the normal phone WebSocket messages above.

The relay currently limits a handshake to 16 KiB and an envelope to 24 MiB.
