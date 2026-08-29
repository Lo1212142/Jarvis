"""Minimal, bounded watchdog and recovery core.

The watchdog is intentionally narrower and more conservative than the agent. It
can restart/restore approved artifacts, but it cannot grant permissions, fetch
code, or rewrite itself. Deployments should run it as a separate process or
container with a read-only baseline and an independent supervisor.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True, slots=True)
class HealthSnapshot:
    service: str
    healthy: bool
    checked_at: float
    reason: str = ""


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    attempted: bool
    restored: bool
    reason: str
    attempts: int


class RecoveryCore:
    """Independent recovery helper for approved paths only."""

    def __init__(
        self,
        *,
        baseline_dir: str | Path,
        state_dir: str | Path,
        max_recovery_attempts: int = 2,
    ) -> None:
        self.baseline_dir = Path(baseline_dir).expanduser().resolve()
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.max_recovery_attempts = max(1, min(5, int(max_recovery_attempts)))
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._last_health: dict[str, HealthSnapshot] = {}
        self._recovery_attempts: dict[str, int] = {}

    def record_health(self, service: str, healthy: bool, reason: str = "") -> HealthSnapshot:
        snapshot = HealthSnapshot(service, healthy, time.time(), reason)
        with self._lock:
            self._last_health[service] = snapshot
        return snapshot

    def health(self) -> list[HealthSnapshot]:
        with self._lock:
            return list(self._last_health.values())

    def baseline_digest(self, relative_path: str) -> str:
        source = self._approved_baseline_path(relative_path)
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        manifest = self.state_dir / "baseline-digests.json"
        data: dict[str, str] = {}
        if manifest.exists():
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
        data[relative_path] = digest
        manifest.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return digest

    def restore_file(self, relative_path: str, target_root: str | Path) -> RecoveryResult:
        """Restore one allowlisted file from baseline with bounded attempts."""
        source = self._approved_baseline_path(relative_path)
        target_root_path = Path(target_root).expanduser().resolve()
        target = (target_root_path / relative_path).resolve()
        if target != target_root_path and target_root_path not in target.parents:
            raise ValueError("target path escapes target_root")
        service_key = str(target)
        with self._lock:
            attempts = self._recovery_attempts.get(service_key, 0)
            if attempts >= self.max_recovery_attempts:
                return RecoveryResult(True, False, "recovery attempt limit reached", attempts)
            self._recovery_attempts[service_key] = attempts + 1
        target.parent.mkdir(parents=True, exist_ok=True)
        temp_target = target.with_suffix(target.suffix + ".recovery.tmp")
        shutil.copyfile(source, temp_target)
        temp_target.replace(target)
        return RecoveryResult(True, True, "restored from approved baseline", attempts + 1)

    def reset_attempts(self, target: str | Path) -> None:
        with self._lock:
            self._recovery_attempts.pop(str(Path(target).expanduser().resolve()), None)

    def _approved_baseline_path(self, relative_path: str) -> Path:
        candidate = (self.baseline_dir / relative_path).resolve()
        if candidate != self.baseline_dir and self.baseline_dir not in candidate.parents:
            raise ValueError("baseline path escapes baseline_dir")
        if not candidate.is_file() or candidate.is_symlink():
            raise ValueError("baseline file is missing or unsafe")
        return candidate


__all__ = ["HealthSnapshot", "RecoveryCore", "RecoveryResult"]
