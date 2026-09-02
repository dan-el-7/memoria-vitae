# Current limitations

- The Android client is a working debug APK but has not been exercised on a
  physical device in this environment. Camera behavior, GPS availability,
  notification permission, battery policy, and background execution still need
  real-device validation.
- The Android UI uses XML views. It does not yet implement the attached plan's
  Compose UI or QR pairing flow.
- Android currently connects directly to the desktop WebSocket. It does not yet
  implement the relay's TCP envelope protocol; the relay path is available to
  desktop plus compatible clients and is covered by relay integration tests.
- Live synthetic replay through FastAPI was not run here because the sandbox
  could not launch the project's compiled Python 3.14 environment. Unit tests
  and relay tests run with the bundled Python runtime plus the project's pure
  Python packages.
- The relay's attach step currently uses the channel id without a separate
  short-lived phone attach secret. Add that before exposing the service to
  untrusted networks.
- Relay TLS is hop-by-hop. Payloads are opaque to the relay but are not
  end-to-end encrypted between phone and desktop.
- The desktop UI is intentionally vanilla HTML/CSS/JavaScript because Node is
  not installed; it is not the React/Vite UI described in the reference plan.
- Ollama model names and VRAM behavior depend on the local Ollama installation.
  The default `qwen3-vl:2b` must be pulled separately.
- The project-local `android/.offline-m2` mirror is an environment-specific
  build aid generated from the existing Gradle cache. A portable checkout should
  use normal Maven access or regenerate the mirror.
