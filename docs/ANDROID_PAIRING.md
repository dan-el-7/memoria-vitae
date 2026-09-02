# Android pairing and first run

1. Start the desktop and open the dashboard.
2. Generate a fresh pairing code. Codes are six characters, expire after ten
   minutes, and can be used only once.
3. In the Android app, enter the desktop base URL and pairing code. The desktop
   returns a device id and token; the app stores the token in private app
   preferences.
4. Grant camera, location, and notification permissions when requested.
5. Start a desktop run. The phone connects with its device token and waits for
   `welcome`. It only uploads images when `welcome.run_id` is non-null.
6. Confirm the dashboard shows the device id and increasing sequence numbers.

Tokens are verified against SHA-256 hashes in the desktop data directory; the
raw token is not stored by the desktop. Revoking a device immediately blocks
future hello messages. The current Android app uses direct WebSocket/LAN
connection. QR pairing, relay URL handling, and physical-device validation are
follow-up work.
