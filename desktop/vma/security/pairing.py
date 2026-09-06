"""Device pairing and authentication.

Flow:
1. Desktop UI generates a short-lived pairing code (GET /api/pairing/code).
2. Phone posts {code, device_name} to POST /api/pair (LAN) or via the relay.
3. On success the phone receives a device_token + challenge-response secret;
   the server stores only a SHA-256 hash of the token plus the CR secret
   sealed with Fernet (auth.key). Subsequent sensor connections prove
   possession of the secret via an HMAC challenge-response — the secret never
   crosses the wire again after pairing. Connections may then upgrade to
   end-to-end AES-256-GCM (see auth_crypto).

Mutual approval: a phone may also *request* pairing (POST /api/pair/request)
without a code; the request sits pending until the desktop human approves it
(POST /api/pair/approve from the dashboard), then the phone completes the
pairing by showing the desktop-generated code, or via a one-time approval
token delivered by the desktop operator out of band.

Tokens never expire (they identify a physical device) but can be revoked
from the UI. The pairing code expires in 10 minutes and is single-use.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..utils import random_code, random_token, read_json, write_json
from .auth_crypto import (
    AuthKeyStore,
    new_pairing_secret,
    new_device_token,
    token_hash,
    verify_response,
)

CODE_TTL_S = 600
APPROVAL_TTL_S = 300          # pending pairing requests live 5 minutes
ATTACH_SECRET_TTL_S = 3600    # relay attach secrets are short-lived


@dataclass
class PairingCode:
    code: str
    created_mono: float
    used: bool = False

    def expired(self) -> bool:
        return (time.monotonic() - self.created_mono) > CODE_TTL_S


@dataclass
class PendingRequest:
    request_id: str
    device_name: str
    created_mono: float
    code: str  # the code the desktop wants the phone to enter (mutual proof)

    def expired(self) -> bool:
        return (time.monotonic() - self.created_mono) > APPROVAL_TTL_S


class PairingManager:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._devices_file = data_dir / "devices.json"
        self._code: PairingCode | None = None
        self._pending: dict[str, PendingRequest] = {}
        self.keystore = AuthKeyStore(data_dir)

    # ------------------------------------------------------------- codes

    def new_code(self) -> str:
        self._code = PairingCode(code=random_code(), created_mono=time.monotonic())
        return self._code.code

    def current_code(self) -> str | None:
        if self._code and not self._code.used and not self._code.expired():
            return self._code.code
        return None

    # ------------------------------------------------- pending approvals

    def create_pairing_request(self, device_name: str) -> dict[str, Any]:
        """Phone-side initiation: 'I want to pair, please approve me.'

        The desktop shows the request; approving it hands the phone the
        desktop's current pairing code (mutual human approval). The phone
        then POSTs /api/pair with that code like the normal flow.
        """
        request_id = random_token(9)
        # Approving hands out the CURRENT code; generate one if none is live.
        code = self.current_code() or self.new_code()
        self._pending[request_id] = PendingRequest(
            request_id=request_id,
            device_name=(device_name or "android device")[:80],
            created_mono=time.monotonic(),
            code=code,
        )
        return {"request_id": request_id, "expires_in_s": APPROVAL_TTL_S}

    def list_pending(self) -> list[dict[str, Any]]:
        self._gc_pending()
        return [
            {"request_id": r.request_id, "device_name": r.device_name,
             "age_s": int(time.monotonic() - r.created_mono)}
            for r in self._pending.values()
        ]

    def approve_request(self, request_id: str) -> dict[str, Any] | None:
        """Desktop human approves: returns the code the phone must show/enter."""
        self._gc_pending()
        req = self._pending.pop(request_id, None)
        if req is None:
            return None
        # Handing out the current (or the request's) code keeps it single-use
        # at /api/pair; the human on the desktop side has now approved.
        code = self.current_code() or req.code
        return {"request_id": request_id, "code": code, "expires_in_s": CODE_TTL_S}

    def deny_request(self, request_id: str) -> bool:
        return self._pending.pop(request_id, None) is not None

    def _gc_pending(self) -> None:
        self._pending = {k: v for k, v in self._pending.items() if not v.expired()}

    # ----------------------------------------------------------- devices

    def pair(self, code: str, device_name: str) -> dict[str, Any] | None:
        """Validate the code, register the device, return its token (once).

        Legacy-compatible: returns token + device_id (+ cr_secret when the
        keystore is available). Callers that ignore cr_secret keep working.
        """
        if not self._code or self._code.used or self._code.expired():
            return None
        if code.strip().upper() != self._code.code:
            return None
        self._code.used = True
        token = new_device_token()
        secret = new_pairing_secret()
        devices = self._load_devices()
        device_id = f"dev_{random_token(8)}"
        entry: dict[str, Any] = {
            "name": device_name or "android device",
            "token_hash": token_hash(token),
            "paired_at": int(time.time()),
            "revoked": False,
        }
        try:
            entry["cr_secret"] = self.keystore.seal_secret(device_id, secret)
        except RuntimeError:
            pass  # keystore unavailable — bearer-token auth only
        devices[device_id] = entry
        write_json(self._devices_file, devices)
        result: dict[str, Any] = {"device_id": device_id, "token": token}
        if "cr_secret" in entry:
            result["cr_secret"] = secret
        return result

    def verify_token(self, token: str) -> str | None:
        """Return device_id for a valid token, else None."""
        if not token:
            return None
        h = token_hash(token)
        for device_id, info in self._load_devices().items():
            if info.get("token_hash") == h and not info.get("revoked"):
                return device_id
        return None

    def cr_secret_for(self, device_id: str) -> str | None:
        """Unseal the challenge-response secret for a device (None = legacy
        device paired before CR support, or keystore failure)."""
        info = self._load_devices().get(device_id)
        if not info:
            return None
        sealed = info.get("cr_secret")
        if not sealed:
            return None
        return self.keystore.open_secret(device_id, str(sealed))

    def verify_challenge(self, device_id: str, server_nonce: str,
                         client_nonce: str, response: str) -> bool:
        secret = self.cr_secret_for(device_id)
        if secret is None:
            return False
        return verify_response(secret, server_nonce, client_nonce, response)

    def rotate_device_secret(self, device_id: str) -> str | None:
        """Issue a fresh CR secret for a device (compromise response)."""
        info = self._load_devices().get(device_id)
        if not info:
            return None
        secret = new_pairing_secret()
        info["cr_secret"] = secret
        devices = self._load_devices()
        devices[device_id] = info
        try:
            devices[device_id]["cr_secret"] = self.keystore.seal_secret(device_id, secret)
        except RuntimeError:
            return None
        return secret

    # ------------------------------------------------------ attach secrets

    def new_attach_secret(self) -> str:
        """Short-lived secret the desktop injects into the relay channel so a
        phone can prove it attaches to the right desktop. Not the device
        credential; single purpose, short TTL."""
        return random_token(24)

    # ----------------------------------------------------------- listing

    def list_devices(self) -> list[dict[str, Any]]:
        out = []
        for device_id, info in self._load_devices().items():
            out.append({"device_id": device_id, "name": info.get("name"),
                        "paired_at": info.get("paired_at"), "revoked": info.get("revoked", False),
                        "auth": "cr+e2e" if info.get("cr_secret") else "bearer"})
        return out

    def revoke(self, device_id: str) -> bool:
        devices = self._load_devices()
        if device_id not in devices:
            return False
        devices[device_id]["revoked"] = True
        write_json(self._devices_file, devices)
        return True

    def unpair_all(self) -> None:
        write_json(self._devices_file, {})

    def _load_devices(self) -> dict[str, Any]:
        if self._devices_file.exists():
            try:
                return read_json(self._devices_file)
            except (OSError, ValueError):
                return {}
        return {}


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
