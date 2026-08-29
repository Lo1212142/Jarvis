"""Server-side audio playback coordination for remote clients.

The server never pretends that a remote speaker played a track.  It issues a
bounded command and waits for a client acknowledgement.  Source files must be
inside explicitly configured roots and are streamed without re-encoding so the
client receives the original quality.
"""

from __future__ import annotations

import mimetypes
import secrets
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

_ALLOWED_AUDIO_SUFFIXES = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".webm"}


@dataclass(frozen=True, slots=True)
class PlaybackState:
    client_id: str
    state: str
    track_id: str = ""
    title: str = ""
    volume: int = 70
    sequence: int = 0
    acknowledged_sequence: int = 0
    last_error: str = ""
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class StreamGrant:
    token: str
    track_id: str
    expires_at: float


class AudioPlaybackService:
    """Coordinate remote playback while preserving truthful acknowledgement."""

    def __init__(self, *, allowed_roots: list[str] | None = None, token_ttl_seconds: float = 120.0) -> None:
        roots = allowed_roots or []
        self._roots = tuple(Path(root).expanduser().resolve() for root in roots if str(root).strip())
        self._token_ttl = max(10.0, min(float(token_ttl_seconds), 900.0))
        self._tracks: dict[str, Path] = {}
        self._grants: dict[str, StreamGrant] = {}
        self._states: dict[str, PlaybackState] = {}
        self._clients: dict[str, Callable[[dict[str, Any]], None]] = {}
        self._lock = threading.RLock()

    def configure_roots(self, allowed_roots: list[str]) -> None:
        with self._lock:
            self._roots = tuple(Path(root).expanduser().resolve() for root in allowed_roots if str(root).strip())
            self._tracks.clear()
            self._grants.clear()

    def roots(self) -> list[str]:
        with self._lock:
            return [str(root) for root in self._roots]

    def register_client(self, client_id: str, sender: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
        client_id = self._safe_id(client_id)
        with self._lock:
            self._states.setdefault(client_id, PlaybackState(client_id=client_id, state="connected", updated_at=time.time()))
            if sender is not None:
                self._clients[client_id] = sender
            state = self._states[client_id]
        return {"client_id": client_id, "connected": True, "state": state.to_dict()}

    def unregister_client(self, client_id: str) -> bool:
        with self._lock:
            existed = client_id in self._clients or client_id in self._states
            self._clients.pop(client_id, None)
            if client_id in self._states:
                state = self._states[client_id]
                self._states[client_id] = PlaybackState(**{**state.to_dict(), "state": "disconnected", "updated_at": time.time()})
            return existed

    def register_track(self, path: str, *, title: str = "") -> dict[str, Any]:
        resolved = self._authorized_path(path)
        if resolved.suffix.lower() not in _ALLOWED_AUDIO_SUFFIXES:
            raise ValueError("unsupported audio format")
        track_id = uuid.uuid4().hex
        with self._lock:
            self._tracks[track_id] = resolved
        return {"track_id": track_id, "title": title or resolved.stem, "format": resolved.suffix.lower().lstrip("."), "size_bytes": resolved.stat().st_size}

    def play(self, client_id: str, track_id: str, *, title: str = "") -> dict[str, Any]:
        client_id = self._safe_id(client_id)
        with self._lock:
            self._require_client(client_id)
            path = self._tracks.get(track_id)
            if path is None:
                raise KeyError("track is not registered")
            self._authorized_path(str(path))
            token = secrets.token_urlsafe(32)
            expires_at = time.time() + self._token_ttl
            self._grants[token] = StreamGrant(token, track_id, expires_at)
            previous = self._states[client_id]
            sequence = previous.sequence + 1
            state = PlaybackState(client_id, "commanded", track_id, title or path.stem, previous.volume, sequence, previous.acknowledged_sequence, "", time.time())
            self._states[client_id] = state
            command = {
                "type": "audio.play",
                "protocol": 1,
                "sequence": sequence,
                "track_id": track_id,
                "title": state.title,
                "stream_path": f"/api/audio/stream/{token}",
                "content_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                "volume": state.volume,
                "expires_at": expires_at,
            }
            sender = self._clients.get(client_id)
        self._send(sender, command)
        return {"accepted": True, "acknowledged": False, "command": command, "state": state.to_dict()}

    def control(self, client_id: str, action: str, *, value: int | None = None) -> dict[str, Any]:
        client_id = self._safe_id(client_id)
        allowed = {"pause", "resume", "stop", "next", "previous", "volume_up", "volume_down", "set_volume"}
        if action not in allowed:
            raise ValueError("unsupported playback action")
        with self._lock:
            self._require_client(client_id)
            previous = self._states[client_id]
            volume = previous.volume
            if action == "volume_up":
                volume = min(100, volume + 10)
            elif action == "volume_down":
                volume = max(0, volume - 10)
            elif action == "set_volume":
                if value is None or value < 0 or value > 100:
                    raise ValueError("volume must be between 0 and 100")
                volume = int(value)
            sequence = previous.sequence + 1
            state_name = "paused" if action == "pause" else "playing" if action == "resume" else "stopped" if action == "stop" else previous.state
            state = PlaybackState(client_id, state_name, previous.track_id, previous.title, volume, sequence, previous.acknowledged_sequence, "", time.time())
            self._states[client_id] = state
            command = {"type": f"audio.{action}", "protocol": 1, "sequence": sequence, "value": volume if action in {"volume_up", "volume_down", "set_volume"} else None}
            sender = self._clients.get(client_id)
        self._send(sender, command)
        return {"accepted": True, "acknowledged": False, "command": command, "state": state.to_dict()}

    def acknowledge(self, client_id: str, sequence: int, *, state: str = "playing", error: str = "") -> dict[str, Any]:
        with self._lock:
            self._require_client(client_id)
            previous = self._states[client_id]
            if sequence < previous.acknowledged_sequence:
                return previous.to_dict()
            next_state = PlaybackState(client_id, "error" if error else state, previous.track_id, previous.title, previous.volume, previous.sequence, sequence, error[:500], time.time())
            self._states[client_id] = next_state
            return next_state.to_dict()

    def state(self, client_id: str | None = None) -> list[dict[str, Any]] | dict[str, Any]:
        with self._lock:
            if client_id is None:
                return [state.to_dict() for state in self._states.values()]
            self._require_client(client_id)
            return self._states[client_id].to_dict()

    def resolve_grant(self, token: str) -> tuple[Path, str]:
        now = time.time()
        with self._lock:
            grant = self._grants.get(token)
            if grant is None or grant.expires_at < now:
                self._grants.pop(token, None)
                raise KeyError("stream token expired or invalid")
            path = self._tracks.get(grant.track_id)
            if path is None:
                raise KeyError("track is no longer registered")
            self._authorized_path(str(path))
            return path, mimetypes.guess_type(path.name)[0] or "application/octet-stream"

    def _authorized_path(self, raw_path: str) -> Path:
        path = Path(raw_path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError("audio file was not found")
        if not self._roots:
            raise PermissionError("no authorized audio root is configured")
        if not any(path == root or root in path.parents for root in self._roots):
            raise PermissionError("audio path is outside the authorized roots")
        return path

    def _require_client(self, client_id: str) -> None:
        if client_id not in self._states:
            raise KeyError("audio client is not registered")

    @staticmethod
    def _safe_id(client_id: str) -> str:
        value = str(client_id).strip()
        if not value or len(value) > 128:
            raise ValueError("client_id is required and must be at most 128 characters")
        return value

    @staticmethod
    def _send(sender: Callable[[dict[str, Any]], None] | None, command: dict[str, Any]) -> None:
        if sender is not None:
            try:
                sender(command)
            except Exception:
                pass


_default_service = AudioPlaybackService()


def get_default_audio_service() -> AudioPlaybackService:
    """Return the process-local coordinator shared by API and built-in tools."""
    return _default_service


__all__ = ["AudioPlaybackService", "PlaybackState", "StreamGrant", "get_default_audio_service"]
