"""Short-lived QR pairing and scoped device registry."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ALLOWED_DEVICE_SCOPES = frozenset({"chat", "events", "read", "camera", "audio", "jobs", "reminders", "browser", "media", "approvals", "logs"})
DEFAULT_DEVICE_SCOPES = frozenset({"chat", "events", "read", "camera", "audio", "jobs", "reminders", "browser", "media"})


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class PairingRequest:
    pairing_id: str
    token_hash: str
    qr_data: str
    server_url: str
    fingerprint: str
    expires_at: float
    scopes: frozenset[str]
    consumed: bool = False


@dataclass(slots=True)
class DeviceRecord:
    device_id: str
    name: str
    token_hash: str
    scopes: frozenset[str]
    created_at: float
    last_seen_at: float
    revoked: bool = False

    def public_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "name": self.name,
            "scopes": sorted(self.scopes),
            "created_at": self.created_at,
            "last_seen_at": self.last_seen_at,
            "revoked": self.revoked,
        }

    def storage_dict(self) -> dict[str, Any]:
        return {**self.public_dict(), "token_hash": self.token_hash}


class PairingService:
    def __init__(self, *, server_url: str = "", ttl_seconds: int = 120, storage_path: str | Path | None = None) -> None:
        self.server_url = server_url.rstrip("/")
        self.ttl_seconds = max(30, min(int(ttl_seconds), 600))
        self.storage_path = Path(storage_path) if storage_path else None
        self._pairings: dict[str, PairingRequest] = {}
        self._devices: dict[str, DeviceRecord] = {}
        self._lock = threading.RLock()
        self._load_devices()

    def create(
        self,
        *,
        server_url: str | None = None,
        scopes: set[str] | frozenset[str] | None = None,
        device_name_hint: str = "",
    ) -> dict[str, Any]:
        with self._lock:
            pairing_id = f"pair_{secrets.token_urlsafe(12)}"
            raw_token = secrets.token_urlsafe(32)
            selected = frozenset(scopes or DEFAULT_DEVICE_SCOPES) & ALLOWED_DEVICE_SCOPES
            if not selected:
                selected = DEFAULT_DEVICE_SCOPES
            base_url = (server_url or self.server_url).rstrip("/")
            expires_at = time.time() + self.ttl_seconds
            fingerprint = _hash(base_url or "openjarvis")[:24]
            safe_name_hint = device_name_hint.strip()[:128]
            qr_payload = {"v": 1, "pairing_id": pairing_id, "server_url": base_url, "token": raw_token, "fingerprint": fingerprint, "expires_at": expires_at, "scopes": sorted(selected), "device_name_hint": safe_name_hint}
            qr_data = json.dumps(qr_payload, separators=(",", ":"), sort_keys=True)
            self._pairings[pairing_id] = PairingRequest(pairing_id, _hash(raw_token), qr_data, base_url, fingerprint, expires_at, selected)
            self._purge_expired()
            return {"pairing_id": pairing_id, "server_url": base_url, "fingerprint": fingerprint, "expires_at": expires_at, "scopes": sorted(selected), "device_name_hint": safe_name_hint}

    def qr_data_for(self, pairing_id: str) -> str | None:
        """Return QR content only for a current, unconsumed pairing renderer.

        The raw short-lived token stays server-side and is never part of the
        administrative JSON response consumed by the browser settings UI.
        """
        with self._lock:
            self._purge_expired()
            request = self._pairings.get(pairing_id)
            if request is None or request.consumed:
                return None
            return request.qr_data

    def consume(self, *, pairing_id: str, token: str, device_name: str, scopes: set[str] | frozenset[str] | None = None) -> dict[str, Any]:
        with self._lock:
            request = self._pairings.get(pairing_id)
            if request is None or request.consumed or request.expires_at <= time.time() or not secrets.compare_digest(request.token_hash, _hash(token)):
                raise ValueError("pairing token is invalid, expired, or already used")
            if not device_name.strip() or len(device_name) > 128:
                raise ValueError("device_name is required and must be at most 128 characters")
            selected = request.scopes & (frozenset(scopes or request.scopes)) & ALLOWED_DEVICE_SCOPES
            if not selected:
                raise ValueError("requested scopes are not allowed")
            now = time.time()
            device_id = f"device_{secrets.token_urlsafe(12)}"
            device_token = f"oj_dev_{secrets.token_urlsafe(32)}"
            self._devices[device_id] = DeviceRecord(device_id, device_name.strip(), _hash(device_token), selected, now, now)
            request.consumed = True
            self._persist_devices()
            return {"device_id": device_id, "device_token": device_token, "scopes": sorted(selected), "server_url": request.server_url, "fingerprint": request.fingerprint}

    def authenticate(self, token: str, *, required_scope: str | None = None) -> DeviceRecord | None:
        token_hash = _hash(token)
        with self._lock:
            for device in self._devices.values():
                if device.revoked or not secrets.compare_digest(device.token_hash, token_hash):
                    continue
                if required_scope and required_scope not in device.scopes:
                    return None
                device.last_seen_at = time.time()
                return device
        return None

    def list_devices(self) -> list[dict[str, Any]]:
        with self._lock:
            return [device.public_dict() for device in self._devices.values()]

    def revoke(self, device_id: str) -> bool:
        with self._lock:
            device = self._devices.get(device_id)
            if device is None:
                return False
            device.revoked = True
            self._persist_devices()
            return True

    def revoke_authenticated(self, token: str) -> DeviceRecord | None:
        """Revoke exactly the active device represented by *token*.

        This deliberately cannot revoke an arbitrary device ID and is used by
        an explicit local privacy/kill-switch action on a paired client.
        """
        token_hash = _hash(token)
        with self._lock:
            for device in self._devices.values():
                if device.revoked or not secrets.compare_digest(device.token_hash, token_hash):
                    continue
                device.revoked = True
                self._persist_devices()
                return device
        return None

    def _load_devices(self) -> None:
        if self.storage_path is None or not self.storage_path.exists():
            return
        try:
            payload = json.loads(self.storage_path.read_text(encoding="utf-8"))
            for raw in payload if isinstance(payload, list) else []:
                if not isinstance(raw, dict):
                    continue
                self._devices[str(raw["device_id"])] = DeviceRecord(str(raw["device_id"]), str(raw.get("name", ""))[:128], str(raw["token_hash"]), frozenset(raw.get("scopes", [])) & ALLOWED_DEVICE_SCOPES, float(raw.get("created_at", 0)), float(raw.get("last_seen_at", 0)), bool(raw.get("revoked", False)))
        except (OSError, ValueError, TypeError, KeyError):
            self._devices = {}

    def _persist_devices(self) -> None:
        if self.storage_path is None:
            return
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.storage_path.with_suffix(self.storage_path.suffix + ".tmp")
        temporary.write_text(json.dumps([device.storage_dict() for device in self._devices.values()], separators=(",", ":")), encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.storage_path)
        os.chmod(self.storage_path, 0o600)

    def _purge_expired(self) -> None:
        now = time.time()
        for pairing_id, request in list(self._pairings.items()):
            if request.expires_at <= now or request.consumed:
                self._pairings.pop(pairing_id, None)


__all__ = ["ALLOWED_DEVICE_SCOPES", "DEFAULT_DEVICE_SCOPES", "DeviceRecord", "PairingRequest", "PairingService"]
