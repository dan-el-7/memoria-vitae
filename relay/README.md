# VMA relay

This small TCP service lets a desktop make an outbound connection so a phone
can reach it without opening the desktop firewall. The relay only forwards
opaque envelopes; the desktop still authenticates the phone's VMA device token
inside the normal `hello` message.

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

The current MVP channel id is an opaque desktop-generated identifier. Phone
device-token authentication remains end-to-end with the desktop; relay TLS is
hop-by-hop. Add a separate attach/pairing secret before exposing a relay to
untrusted users.

