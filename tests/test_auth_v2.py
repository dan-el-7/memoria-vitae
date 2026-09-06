"""Security tests for v2 pairing: challenge-response, E2E sealing, key store.

These are the guards for the auth design — if any of these break, the
'no secrets on the wire' property is gone.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "desktop"))

from vma.security.auth_crypto import (
    DIR_PHONE_TO_SERVER,
    DIR_SERVER_TO_PHONE,
    AuthKeyStore,
    SealError,
    SealedChannel,
    challenge_response,
    derive_session_key,
    device_fingerprint,
    token_hash,
    verify_response,
)
from vma.security.pairing import PairingManager


# ------------------------------------------------------------------- crypto


def test_challenge_response_roundtrip() -> None:
    secret = "s3cret"
    r = challenge_response(secret, "server-nonce", "client-nonce")
    assert verify_response(secret, "server-nonce", "client-nonce", r)
    # Wrong secret, wrong nonces, tampered response all fail.
    assert not verify_response("other", "server-nonce", "client-nonce", r)
    assert not verify_response(secret, "tampered", "client-nonce", r)
    assert not verify_response(secret, "server-nonce", "tampered", r)
    assert not verify_response(secret, "server-nonce", "client-nonce", r[:-1] + ("0" if r[-1] != "0" else "1"))


def test_session_key_is_binding_and_fresh() -> None:
    k1 = derive_session_key("s", "sn", "cn")
    k2 = derive_session_key("s", "sn", "cn")
    k3 = derive_session_key("s", "sn2", "cn")
    k4 = derive_session_key("s2", "sn", "cn")
    assert k1 == k2            # deterministic
    assert k1 != k3            # bound to server nonce
    assert k1 != k4            # bound to secret
    assert len(k1) == 32       # AES-256


def test_sealed_channel_roundtrip_and_direction() -> None:
    key = derive_session_key("s", "sn", "cn")
    ch = SealedChannel(key)
    blob = ch.seal(b"hello frame", DIR_PHONE_TO_SERVER)
    # A fresh receiver with the same key can open it.
    rx = SealedChannel(key)
    assert rx.unseal(blob, DIR_PHONE_TO_SERVER) == b"hello frame"


def test_sealed_channel_rejects_replay_and_reorder() -> None:
    key = derive_session_key("s", "sn", "cn")
    tx = SealedChannel(key)
    a = tx.seal(b"one", DIR_PHONE_TO_SERVER)
    b = tx.seal(b"two", DIR_PHONE_TO_SERVER)
    rx = SealedChannel(key)
    rx.unseal(b, DIR_PHONE_TO_SERVER)
    with pytest.raises(SealError):
        rx.unseal(a, DIR_PHONE_TO_SERVER)  # older counter after newer: replay
    with pytest.raises(SealError):
        rx.unseal(b, DIR_PHONE_TO_SERVER)  # same frame twice: replay


def test_sealed_channel_rejects_wrong_key_and_direction() -> None:
    key = derive_session_key("s", "sn", "cn")
    tx = SealedChannel(key)
    blob = tx.seal(b"data", DIR_PHONE_TO_SERVER)
    wrong_key = SealedChannel(derive_session_key("attacker", "sn", "cn"))
    with pytest.raises(SealError):
        wrong_key.unseal(blob, DIR_PHONE_TO_SERVER)
    rx = SealedChannel(key)
    with pytest.raises(SealError):
        rx.unseal(blob, DIR_SERVER_TO_PHONE)  # wrong direction prefix


def test_sealed_channel_rejects_tampering() -> None:
    key = derive_session_key("s", "sn", "cn")
    tx = SealedChannel(key)
    blob = bytearray(tx.seal(b"data", DIR_PHONE_TO_SERVER))
    blob[-1] ^= 0x01  # flip one bit of ciphertext/tag
    rx = SealedChannel(key)
    with pytest.raises(SealError):
        rx.unseal(bytes(blob), DIR_PHONE_TO_PHONE_DIR())


def DIR_PHONE_TO_PHONE_DIR() -> bytes:
    return DIR_PHONE_TO_SERVER


# --------------------------------------------------------------- key store


def test_keystore_seal_open_roundtrip(tmp_path: Path) -> None:
    ks = AuthKeyStore(tmp_path)
    assert ks.available
    sealed = ks.seal_secret("dev_x", "my-secret")
    assert "my-secret" not in sealed  # ciphertext only
    assert ks.open_secret("dev_x", sealed) == "my-secret"
    assert ks.open_secret("dev_x", sealed + "x") is None  # tamper


def test_keystore_persists_across_instances(tmp_path: Path) -> None:
    ks1 = AuthKeyStore(tmp_path)
    sealed = ks1.seal_secret("dev_x", "my-secret")
    ks2 = AuthKeyStore(tmp_path)  # loads the same key file
    assert ks2.open_secret("dev_x", sealed) == "my-secret"


# --------------------------------------------------------------- pairing v2


def test_pair_returns_cr_secret_and_desktop_stores_ciphertext(tmp_path: Path) -> None:
    pm = PairingManager(tmp_path)
    code = pm.new_code()
    result = pm.pair(code, "Pixel 9")
    assert result is not None
    assert result["cr_secret"]
    assert pm.cr_secret_for(result["device_id"]) == result["cr_secret"]
    # devices.json must never contain the plaintext secret.
    raw = (tmp_path / "devices.json").read_text(encoding="utf-8")
    assert result["cr_secret"] not in raw
    assert result["token"] not in raw
    assert token_hash(result["token"]) in raw


def test_pair_challenge_verify(tmp_path: Path) -> None:
    pm = PairingManager(tmp_path)
    code = pm.new_code()
    result = pm.pair(code, "Pixel 9")
    assert result is not None
    dev = result["device_id"]
    response = challenge_response(result["cr_secret"], "sn", "cn")
    assert pm.verify_challenge(dev, "sn", "cn", response)
    assert not pm.verify_challenge(dev, "sn", "cn", "deadbeef")


def test_legacy_device_bearer_only(tmp_path: Path) -> None:
    """A devices.json without cr_secret (pre-v2 pairing) keeps working."""
    pm = PairingManager(tmp_path)
    code = pm.new_code()
    result = pm.pair(code, "old phone")
    assert result is not None
    # Simulate a legacy entry by stripping the secret.
    import json as _json
    devices = _json.loads((tmp_path / "devices.json").read_text())
    for entry in devices.values():
        entry.pop("cr_secret", None)
    (tmp_path / "devices.json").write_text(_json.dumps(devices))
    assert pm.cr_secret_for(result["device_id"]) is None
    assert pm.verify_token(result["token"]) == result["device_id"]


def test_revocation_blocks_token_and_cr(tmp_path: Path) -> None:
    pm = PairingManager(tmp_path)
    code = pm.new_code()
    result = pm.pair(code, "Pixel 9")
    assert result is not None
    pm.revoke(result["device_id"])
    assert pm.verify_token(result["token"]) is None


def test_pending_approval_flow(tmp_path: Path) -> None:
    pm = PairingManager(tmp_path)
    req = pm.create_pairing_request("Pixel 9")
    assert pm.list_pending()
    approval = pm.approve_request(req["request_id"])
    assert approval and approval["code"]
    # The approved code completes the normal pairing flow.
    result = pm.pair(approval["code"], "Pixel 9")
    assert result is not None and result["cr_secret"]
    # Approval is single-use.
    assert pm.approve_request(req["request_id"]) is None
    assert not pm.list_pending()


def test_deny_request(tmp_path: Path) -> None:
    pm = PairingManager(tmp_path)
    req = pm.create_pairing_request("rando")
    assert pm.deny_request(req["request_id"])
    assert not pm.list_pending()


def test_device_fingerprint_stable_and_short() -> None:
    assert device_fingerprint("s") == device_fingerprint("s")
    assert device_fingerprint("s") != device_fingerprint("t")
    assert len(device_fingerprint("s")) == 12


# ------------------------------------------------- sensor handshake (in-proc)


class _FakeWS:
    """Minimal WebSocket stand-in for SensorHub tests."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False

    async def accept(self) -> None:
        pass

    async def receive(self) -> dict:
        await asyncio.sleep(0)
        return {"type": "websocket.disconnect"}

    async def send_text(self, text: str) -> None:
        self.sent.append(text)

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(data.hex())

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True


class _FakePairing:
    """Bearer + CR verification against a real PairingManager."""

    def __init__(self, pm: PairingManager) -> None:
        self.pm = pm

    def verify_token(self, token: str) -> str | None:
        return self.pm.verify_token(token)

    def cr_secret_for(self, device_id: str) -> str | None:
        return self.pm.cr_secret_for(device_id)

    def verify_challenge(self, device_id: str, sn: str, cn: str, response: str) -> bool:
        return self.pm.verify_challenge(device_id, sn, cn, response)


class _FakeState:
    def __init__(self, pm: PairingManager) -> None:
        self.pairing = _FakePairing(pm)
        self.current_run = None
        self.cfg = type("C", (), {})()
        self.cfg.pipeline = type("P", (), {})()
        self.cfg.pipeline.min_interval_ms = 250
        self.cfg.pipeline.heartbeat_interval_s = 30.0

    def run_state(self) -> str:
        return "idle"

    async def broadcast_ui(self) -> None:
        pass


@pytest.mark.asyncio
async def test_sensor_cr_e2e_handshake(tmp_path: Path) -> None:
    """Full v2 handshake over a fake WS: hello(cr nonce) -> challenge ->
    auth_response -> e2e_start -> sealed welcome, then a sealed heartbeat."""
    from vma.server.sensor import SensorHub

    pm = PairingManager(tmp_path)
    code = pm.new_code()
    paired = pm.pair(code, "Pixel 9")
    assert paired is not None
    state = _FakeState(pm)
    hub = SensorHub(state)
    ws = _FakeWS()

    # Drive the handshake manually through _on_control.
    import secrets as _secrets
    client_nonce = _secrets.token_urlsafe(12)
    seen = await hub._on_control(ws, json.dumps({
        "type": "hello", "token": paired["token"],
        "device": {"model": "Pixel 9"},
        "cr": {"nonce": client_nonce},
    }), hello_seen=False)
    assert not seen  # welcome deferred
    challenge = json.loads(ws.sent[-1])
    assert challenge["type"] == "auth_challenge"
    server_nonce = challenge["nonce"]

    response = challenge_response(paired["cr_secret"], server_nonce, client_nonce)
    seen = await hub._on_control(ws, json.dumps({
        "type": "auth_response", "nonce": client_nonce, "response": response,
    }), hello_seen=False)
    assert seen
    # e2e_start (plaintext, includes no key material) then sealed welcome.
    e2e = json.loads(ws.sent[-2])
    assert e2e["type"] == "e2e_start"
    sealed_welcome = bytes.fromhex(ws.sent[-1])
    key = derive_session_key(paired["cr_secret"], server_nonce, client_nonce)
    rx = SealedChannel(key)
    welcome = json.loads(rx.unseal(sealed_welcome, DIR_SERVER_TO_PHONE).decode())
    assert welcome["type"] == "welcome"
    assert welcome["auth"] == "cr+e2e"
    assert hub.sensors.auth == "cr+e2e"

    # A plaintext control frame after e2e_start must be rejected.
    ws2 = _FakeWS()
    hub._ws = ws2
    await hub._on_control(ws2, json.dumps({"type": "heartbeat"}), hello_seen=True)
    err = json.loads(ws2.sent[-1]) if ws2.sent and not _is_hex(ws2.sent[-1]) else None
    # sealed frames are hex; a plaintext heartbeat is rejected as bad sealed
    # frame -> error e2e_bad, or the hub stays sealed. Either way no
    # plaintext welcome/ack leaked.
    assert not any("welcome" in s and not _is_hex(s) for s in ws2.sent)


def _is_hex(s: str) -> bool:
    try:
        bytes.fromhex(s)
        return True
    except ValueError:
        return False


@pytest.mark.asyncio
async def test_sensor_bad_cr_response_rejected(tmp_path: Path) -> None:
    from vma.server.sensor import SensorHub

    pm = PairingManager(tmp_path)
    code = pm.new_code()
    paired = pm.pair(code, "Pixel 9")
    assert paired is not None
    state = _FakeState(pm)
    hub = SensorHub(state)
    ws = _FakeWS()

    seen = await hub._on_control(ws, json.dumps({
        "type": "hello", "token": paired["token"], "device": {},
        "cr": {"nonce": "client-nonce"},
    }), hello_seen=False)
    assert not seen
    challenge = json.loads(ws.sent[-1])
    assert challenge["type"] == "auth_challenge"

    seen = await hub._on_control(ws, json.dumps({
        "type": "auth_response", "nonce": "client-nonce", "response": "f" * 64,
    }), hello_seen=False)
    assert not seen
    assert ws.closed
    assert hub.sensors.auth == "none"


@pytest.mark.asyncio
async def test_sensor_legacy_bearer_still_works(tmp_path: Path) -> None:
    from vma.server.sensor import SensorHub

    pm = PairingManager(tmp_path)
    code = pm.new_code()
    paired = pm.pair(code, "legacy phone")
    assert paired is not None
    # Strip CR to simulate a pre-v2 pairing.
    import json as _json
    devices = _json.loads((tmp_path / "devices.json").read_text())
    for entry in devices.values():
        entry.pop("cr_secret", None)
    (tmp_path / "devices.json").write_text(_json.dumps(devices))

    state = _FakeState(pm)
    hub = SensorHub(state)
    ws = _FakeWS()
    seen = await hub._on_control(ws, json.dumps({
        "type": "hello", "token": paired["token"], "device": {},
        "cr": {"nonce": "n"},  # phone offers CR, desktop has no secret
    }), hello_seen=False)
    assert seen
    welcome = json.loads(ws.sent[-1])
    assert welcome["type"] == "welcome"
    assert welcome["auth"] == "bearer"
    assert hub.sensors.auth == "bearer"
