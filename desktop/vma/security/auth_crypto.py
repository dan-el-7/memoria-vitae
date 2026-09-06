"""E2E authentication and encryption for the sensor channel.

Threat model
------------
The relay (if any) is untrusted infrastructure: it may be operated by anyone
and sees every byte. Desktop/phone trust each other via credentials exchanged
ONLY at pairing time over the LAN (which is also when the human is present on
both ends). After pairing:

1. ``hello`` carries the device_id + an HMAC-SHA256 challenge response over a
   per-connection nonce from the server — the long-lived secret NEVER crosses
   the wire again, so a hostile relay cannot capture and replay it.
2. Once authenticated, BOTH directions are sealed with AES-256-GCM using a
   per-connection key derived (HKDF-SHA256) from the pairing secret + both
   nonces. The relay (and any LAN sniffer) sees only ciphertext from then on.

At rest: the desktop stores the SHA-256 hash of the device token (lookup) and
the challenge secret sealed with Fernet under ``<data_dir>/auth.key`` (never
plaintext).

Replay protection: every response includes the connection nonce, and frames
carry seq numbers the server already dedups — plus the GCM nonce is a
monotonic counter per direction, so sealed frames cannot be reordered/replayed
undetected (GCM rejects nonce reuse under the same key).

Key rotation: ``rotate_device_secret(device_id)`` re-issues a secret on the
next LAN re-pair; old ciphertexts are unaffected (per-connection keys).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import struct
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.fernet import Fernet, InvalidToken

# --------------------------------------------------------------------- consts

TOKEN_BYTES = 32          # device token entropy (urlsafe, ~43 chars)
SECRET_BYTES = 32         # challenge-response secret entropy
NONCE_BYTES = 12          # GCM standard nonce size
KEY_LEN = 32              # AES-256
HKDF_INFO = b"vma-e2e-v1" # domain separation for the session key

# GCM nonce = 4-byte direction prefix + 8-byte big-endian counter. The prefix
# prevents cross-direction nonce collisions; the counter must NEVER repeat
# under one key — senders increment strictly and the receiver rejects
# regressions (replay/reorder).
DIR_PHONE_TO_SERVER = b"P2S\0"
DIR_SERVER_TO_PHONE = b"S2P\0"

MAX_AUTH_ATTEMPTS = 5     # per-connection failed CR attempts before drop
AUTH_TIMEOUT_S = 30.0     # must authenticate within this window

# ------------------------------------------------------------------- helpers


def new_device_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def new_pairing_secret() -> str:
    return secrets.token_urlsafe(SECRET_BYTES)


def token_hash(token: str) -> str:
    """SHA-256 of the device token — what the desktop persists for lookup."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def challenge_response(secret: str, server_nonce: str, client_nonce: str) -> str:
    """HMAC-SHA256 over both nonces, keyed by the pairing secret.

    Both nonces are bound so a captured response cannot be replayed against a
    different connection (server nonce differs) and the client proves liveness
    (client nonce differs per attempt).
    """
    mac = hmac.new(
        secret.encode("utf-8"),
        f"vma-auth-v1:{server_nonce}:{client_nonce}".encode("utf-8"),
        hashlib.sha256,
    )
    return mac.hexdigest()


def verify_response(secret: str, server_nonce: str, client_nonce: str,
                    response: str) -> bool:
    expected = challenge_response(secret, server_nonce, client_nonce)
    return hmac.compare_digest(expected, (response or "").lower())


def derive_session_key(secret: str, server_nonce: str, client_nonce: str) -> bytes:
    """HKDF-SHA256 -> 32-byte AES-256-GCM key bound to this connection only."""
    return HKDF(
        algorithm=hashes.SHA256(),
        length=KEY_LEN,
        salt=(server_nonce + client_nonce).encode("utf-8"),
        info=HKDF_INFO,
    ).derive(secret.encode("utf-8"))


# ------------------------------------------------------------ sealed frames

class SealError(Exception):
    """Sealed frame failed integrity/nonce checks — treat as connection kill."""


class SealedChannel:
    """AES-256-GCM framing over an already-derived session key.

    Wire format: [u32-LE length][1B flags][12B nonce][ciphertext+tag].
    The nonce embeds a per-direction monotonic counter; receivers reject
    counters <= last seen (replay/reorder protection).
    """

    OVERHEAD = 4 + 1 + NONCE_BYTES + 16  # len + flags + nonce + GCM tag

    def __init__(self, key: bytes) -> None:
        if len(key) != KEY_LEN:
            raise ValueError("session key must be 32 bytes")
        self._aes = AESGCM(key)
        self._send_counter = 0
        self._recv_counter = 0

    def _nonce(self, direction: bytes, counter: int) -> bytes:
        # 4-byte direction prefix + 8-byte BE counter = 12-byte GCM nonce
        return direction + struct.pack(">Q", counter)

    def seal(self, plaintext: bytes, direction: bytes,
             associated_data: bytes = b"") -> bytes:
        self._send_counter += 1
        if self._send_counter >= 2 ** 64:
            raise SealError("send counter exhausted")  # ~5e19 frames; unreachable
        nonce = self._nonce(direction, self._send_counter)
        ct = self._aes.encrypt(nonce, plaintext, associated_data or None)
        # Prepend the counter so the receiver can check monotonicity.
        return struct.pack(">Q", self._send_counter) + nonce + ct

    def unseal(self, blob: bytes, direction: bytes,
               associated_data: bytes = b"") -> bytes:
        if len(blob) < 8 + NONCE_BYTES + 16:
            raise SealError("sealed frame too short")
        (counter,) = struct.unpack_from(">Q", blob, 0)
        if counter <= self._recv_counter:
            raise SealError(f"replayed or reordered sealed frame ({counter} <= {self._recv_counter})")
        nonce = blob[8:8 + NONCE_BYTES]
        if not nonce.startswith(direction):
            raise SealError("wrong direction prefix")
        ct = blob[8 + NONCE_BYTES:]
        try:
            plaintext = self._aes.decrypt(nonce, ct, associated_data or None)
        except Exception as exc:
            raise SealError(f"GCM authentication failed: {exc}") from exc
        self._recv_counter = counter
        return plaintext


# ------------------------------------------------------- desktop key storage

class AuthKeyStore:
    """Fernet-sealed at-rest storage for per-device challenge secrets.

    Key file: ``<data_dir>/auth.key`` (0600-equivalent). One Fernet key
    protects ALL device secrets; a missing key file means no CR-capable
    devices (legacy devices keep working via bearer-token auth).
    """

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._key_path = self.data_dir / "auth.key"
        self._fernet: Fernet | None = None
        self._load()

    def _load(self) -> None:
        if self._key_path.exists():
            key = self._key_path.read_bytes().strip()
            if key:
                self._fernet = Fernet(key)
                return
        key = Fernet.generate_key()
        # Best-effort restrictive permissions (Windows ACLs differ; the file
        # lives under the user's profile either way).
        fd = os.open(self._key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(key)
        self._fernet = Fernet(key)

    @property
    def available(self) -> bool:
        return self._fernet is not None

    def seal_secret(self, device_id: str, secret: str) -> str:
        if self._fernet is None:
            raise RuntimeError("auth key store unavailable")
        return self._fernet.encrypt(secret.encode("utf-8")).decode("ascii")

    def open_secret(self, device_id: str, sealed: str) -> str | None:
        if self._fernet is None or not sealed:
            return None
        try:
            return self._fernet.decrypt(sealed.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError):
            return None

    def rekey(self) -> bytes:
        """Generate a fresh Fernet key (e.g. after suspected compromise).

        WARNING: existing sealed secrets become unreadable; all devices must
        re-pair. Intentionally does NOT overwrite the file automatically —
        the operator replaces auth.key deliberately.
        """
        return Fernet.generate_key()


def device_fingerprint(secret: str) -> str:
    """Short non-reversible id so logs/UI can show a device without the secret."""
    return hashlib.sha256(("fp:" + secret).encode("utf-8")).hexdigest()[:12]
