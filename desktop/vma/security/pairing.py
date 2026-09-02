"""Device pairing and authentication.

Flow:
1. Desktop UI generates a short-lived pairing code (GET /api/pairing/code).
2. Phone posts {code, device_name} to POST /api/pair (LAN) or via the relay.
3. On success the phone receives a device_token; the server stores only a
   SHA-256 hash. Subsequent sensor connections present the token in the
   `hello` control message; unknown tokens are rejected before any frame is
   accepted.

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

CODE_TTL_S = 600


@dataclass
class PairingCode:
    code: str
    created_mono: float
    used: bool = False

    def expired(self) -> bool:
        return (time.monotonic() - self.created_mono) > CODE_TTL_S


class PairingManager:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._devices_file = data_dir / "devices.json"
        self._code: PairingCode | None = None

    # ------------------------------------------------------------- codes

    def new_code(self) -> str:
        self._code = PairingCode(code=random_code(), created_mono=time.monotonic())
        return self._code.code

    def current_code(self) -> str | None:
        if self._code and not self._code.used and not self._code.expired():
            return self._code.code
        return None

    # ----------------------------------------------------------- devices

    def pair(self, code: str, device_name: str) -> dict[str, Any] | None:
        """Validate the code, register the device, return its token (once)."""
        if not self._code or self._code.used or self._code.expired():
            return None
        if code.strip().upper() != self._code.code:
            return None
        self._code.used = True
        token = random_token(32)
        devices = self._load_devices()
        device_id = f"dev_{random_token(8)}"
        devices[device_id] = {
            "name": device_name or "android device",
            "token_hash": _hash(token),
            "paired_at": int(time.time()),
            "revoked": False,
        }
        write_json(self._devices_file, devices)
        return {"device_id": device_id, "token": token}

    def verify_token(self, token: str) -> str | None:
        """Return device_id for a valid token, else None."""
        if not token:
            return None
        h = _hash(token)
        for device_id, info in self._load_devices().items():
            if info.get("token_hash") == h and not info.get("revoked"):
                return device_id
        return None

    def list_devices(self) -> list[dict[str, Any]]:
        out = []
        for device_id, info in self._load_devices().items():
            out.append({"device_id": device_id, "name": info.get("name"),
                        "paired_at": info.get("paired_at"), "revoked": info.get("revoked", False)})
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
