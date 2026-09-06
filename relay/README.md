# VMA relay

This small TCP service lets a desktop make an outbound connection so a phone
can reach it without opening the desktop firewall. The relay only forwards
opaque envelopes; the desktop still authenticates the phone's VMA device
token (or challenge-response) in the normal `hello` message.

Since the 2026-09-06 auth v2 update, payloads are **end-to-end encrypted**
between phone and desktop (challenge-response pairing derives an AES-256-GCM
session key; see `desktop/vma/security/auth_crypto.py`). The relay — and its
operator — sees only ciphertext after the initial handshake. TLS on the relay
is still recommended (it hides metadata and the pairing handshake).

## Run

```text
python -m vma_relay.server --host 0.0.0.0 --port 8765 --reg-token change-me
```

The desktop uses `tcp://host:8765` (or `tls://host:8765` with a trusted TLS
certificate) in its `server.relay_url` and the same value as
`server.relay_reg_token`. For deployment, build this directory as a container:

```text
docker build -t vma-relay .
docker run --rm -p 8765:8765 vma-relay --host 0.0.0.0 --reg-token change-me
```

## Phone attachment (v2)

Phones no longer attach by bare channel id. The relay issues an **attach
secret** to the desktop at registration; the desktop shows it inside the
online pairing QR (`mode=online&relay&rport&channel&attach`) and the phone
presents it in its handshake:

```json
{"role": "phone", "channel_id": "desk_…", "attach_secret": "…",
 "wait_for_desktop": true}
```

Behavior:

- Wrong attach secret → rejected before any slot is used.
- `wait_for_desktop: true` parks the phone up to 300 s when the desktop is
  offline (its reconnect loop keeps trying afterwards).
- If the phone presents the previous secret after a relay restart, the relay
  replies `{"type": "attach_secret_refresh", "attach_secret": "…"}` and
  drops the connection; the phone stores the new secret and reconnects.
- Desktop blips do not rotate the secret: a desktop re-registering with the
  secret it already holds keeps it.
- Idle keepalive pings every 25 s; unresponsive peers are dropped ~75 s.
  Channels with no desktop and no waiting phone are reaped after 15 min.
