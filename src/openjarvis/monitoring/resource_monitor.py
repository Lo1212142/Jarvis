"""Truthful CPU/RAM monitoring for the Jarvis server process.

The monitor reports measurements only.  If the operating system does not expose
an observation, the snapshot is marked unavailable instead of returning a made-up
zero.  Alerts are edge-triggered and cooldown-limited so a busy server cannot
flood the user.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from typing import Any, Callable

try:  # Optional dependency; Linux /proc remains the safe fallback.
    import psutil  # type: ignore
except ImportError:  # pragma: no cover - exercised on minimal deployments
    psutil = None  # type: ignore[assignment]


@dataclass(frozen=True, slots=True)
class ResourceSnapshot:
    timestamp: float
    process_cpu_percent: float | None
    process_rss_mb: float | None
    process_memory_percent: float | None
    system_cpu_percent: float | None
    system_memory_percent: float | None
    system_memory_available_mb: float | None
    cpu_count: int | None
    system_memory_total_mb: float | None
    measurement_available: bool
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"errors": list(self.errors)}


class ResourceMonitor:
    """Bounded process/system resource sampler with truthful alerting."""

    def __init__(
        self,
        *,
        poll_interval_seconds: float = 5.0,
        cpu_alert_percent: float = 85.0,
        memory_alert_percent: float = 85.0,
        alert_cooldown_seconds: float = 120.0,
        history_size: int = 120,
        alert_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self.poll_interval_seconds = max(1.0, min(float(poll_interval_seconds), 3600.0))
        self.cpu_alert_percent = max(1.0, min(float(cpu_alert_percent), 1000.0))
        self.memory_alert_percent = max(1.0, min(float(memory_alert_percent), 100.0))
        self.alert_cooldown_seconds = max(0.0, min(float(alert_cooldown_seconds), 86400.0))
        self._history: deque[ResourceSnapshot] = deque(maxlen=max(10, min(int(history_size), 1000)))
        self._alerts: deque[dict[str, Any]] = deque(maxlen=200)
        self._last_alert_at: dict[str, float] = {}
        self._high_state: dict[str, bool] = {"cpu": False, "memory": False}
        self._callback = alert_callback
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._process = psutil.Process(os.getpid()) if psutil is not None else None
        self._proc_cpu_previous: tuple[int, float] | None = None
        self._system_cpu_previous: tuple[int, int, float] | None = None

    def configure(
        self,
        *,
        poll_interval_seconds: float | None = None,
        cpu_alert_percent: float | None = None,
        memory_alert_percent: float | None = None,
        alert_cooldown_seconds: float | None = None,
    ) -> dict[str, float]:
        with self._lock:
            if poll_interval_seconds is not None:
                self.poll_interval_seconds = max(1.0, min(float(poll_interval_seconds), 3600.0))
            if cpu_alert_percent is not None:
                self.cpu_alert_percent = max(1.0, min(float(cpu_alert_percent), 1000.0))
            if memory_alert_percent is not None:
                self.memory_alert_percent = max(1.0, min(float(memory_alert_percent), 100.0))
            if alert_cooldown_seconds is not None:
                self.alert_cooldown_seconds = max(0.0, min(float(alert_cooldown_seconds), 86400.0))
            return self.config()

    def config(self) -> dict[str, float]:
        with self._lock:
            return {
                "poll_interval_seconds": self.poll_interval_seconds,
                "cpu_alert_percent": self.cpu_alert_percent,
                "memory_alert_percent": self.memory_alert_percent,
                "alert_cooldown_seconds": self.alert_cooldown_seconds,
            }

    def current(self) -> ResourceSnapshot:
        with self._lock:
            if self._history:
                return self._history[-1]
        return self.sample()

    def sample(self) -> ResourceSnapshot:
        now = time.time()
        errors: list[str] = []
        process_cpu: float | None = None
        process_rss: float | None = None
        process_memory: float | None = None
        system_cpu: float | None = None
        system_memory: float | None = None
        system_available: float | None = None
        system_total: float | None = None
        cpu_count: int | None = None

        if psutil is not None:
            try:
                assert self._process is not None
                # A short blocking interval produces a real observation for
                # both process and host instead of psutil's initial baseline.
                process_cpu = float(self._process.cpu_percent(interval=0.1))
                system_cpu = float(psutil.cpu_percent(interval=0.1))
                info = self._process.memory_info()
                process_rss = info.rss / (1024 * 1024)
                vm = psutil.virtual_memory()
                process_memory = (info.rss / vm.total * 100.0) if vm.total else None
                system_memory = float(vm.percent)
                system_available = vm.available / (1024 * 1024)
                system_total = vm.total / (1024 * 1024)
                cpu_count = int(psutil.cpu_count() or 0) or None
            except Exception as exc:  # pragma: no cover - defensive OS boundary
                errors.append(f"psutil:{type(exc).__name__}")
        else:
            self._sample_procfs(
                now,
                errors,
                values := {},
            )
            process_cpu = values.get("process_cpu_percent")
            process_rss = values.get("process_rss_mb")
            process_memory = values.get("process_memory_percent")
            system_cpu = values.get("system_cpu_percent")
            system_memory = values.get("system_memory_percent")
            system_available = values.get("system_memory_available_mb")
            system_total = values.get("system_memory_total_mb")
            cpu_count = values.get("cpu_count")

        snapshot = ResourceSnapshot(
            timestamp=now,
            process_cpu_percent=process_cpu,
            process_rss_mb=process_rss,
            process_memory_percent=process_memory,
            system_cpu_percent=system_cpu,
            system_memory_percent=system_memory,
            system_memory_available_mb=system_available,
            cpu_count=cpu_count,
            system_memory_total_mb=system_total,
            measurement_available=process_rss is not None or process_cpu is not None,
            errors=tuple(errors),
        )
        with self._lock:
            self._history.append(snapshot)
            self._evaluate_alerts(snapshot)
        return snapshot

    def _sample_procfs(self, now: float, errors: list[str], values: dict[str, Any]) -> None:
        try:
            page_size = os.sysconf("SC_PAGE_SIZE")
            rss_pages = int(open("/proc/self/statm", encoding="ascii").read().split()[1])
            values["process_rss_mb"] = rss_pages * page_size / (1024 * 1024)
            meminfo = {}
            for line in open("/proc/meminfo", encoding="ascii"):
                key, _, raw = line.partition(":")
                if key in {"MemTotal", "MemAvailable"}:
                    meminfo[key] = float(raw.strip().split()[0])
            total = meminfo.get("MemTotal")
            available = meminfo.get("MemAvailable")
            if total:
                values["system_memory_total_mb"] = total / 1024
                values["system_memory_available_mb"] = available / 1024 if available is not None else None
                values["system_memory_percent"] = (1.0 - (available or 0.0) / total) * 100.0
            values["cpu_count"] = os.cpu_count() or None
            proc_ticks = int(open("/proc/self/stat", encoding="ascii").read().split()[13]) + int(open("/proc/self/stat", encoding="ascii").read().split()[14])
            total_ticks = int(open("/proc/stat", encoding="ascii").readline().split()[1:][0])
            if self._proc_cpu_previous:
                old_ticks, old_time = self._proc_cpu_previous
                elapsed = max(0.001, now - old_time)
                values["process_cpu_percent"] = max(0.0, (proc_ticks - old_ticks) / max(1, os.sysconf("SC_CLK_TCK")) / elapsed * 100.0)
            self._proc_cpu_previous = (proc_ticks, now)
            values["system_cpu_percent"] = None
            _ = total_ticks
        except Exception as exc:  # pragma: no cover - platform fallback
            errors.append(f"procfs:{type(exc).__name__}")

    def _evaluate_alerts(self, snapshot: ResourceSnapshot) -> None:
        checks = (
            ("cpu", snapshot.process_cpu_percent, self.cpu_alert_percent, "CPU"),
            ("memory", snapshot.process_memory_percent, self.memory_alert_percent, "RAM"),
        )
        now = snapshot.timestamp
        for kind, value, threshold, label in checks:
            high = value is not None and value >= threshold
            was_high = self._high_state[kind]
            self._high_state[kind] = high
            if not high or was_high:
                continue
            if now - self._last_alert_at.get(kind, 0.0) < self.alert_cooldown_seconds:
                continue
            alert = {
                "kind": kind,
                "timestamp": now,
                "value": round(float(value), 2),
                "threshold": threshold,
                "message": f"{label} usage is elevated: {float(value):.1f}% (threshold {threshold:.1f}%).",
                "snapshot": snapshot.to_dict(),
            }
            self._last_alert_at[kind] = now
            self._alerts.append(alert)
            if self._callback is not None:
                try:
                    self._callback(dict(alert))
                except Exception:
                    pass

    def history(self, limit: int = 60) -> list[dict[str, Any]]:
        with self._lock:
            bounded = max(1, min(int(limit), len(self._history) or 1))
            return [item.to_dict() for item in list(self._history)[-bounded:]]

    def alerts(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            bounded = max(1, min(int(limit), len(self._alerts) or 1))
            return list(self._alerts)[-bounded:]

    def drain_alerts(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            bounded = max(1, min(int(limit), len(self._alerts)))
            drained = [self._alerts.popleft() for _ in range(bounded)]
            return drained

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="jarvis-resource-monitor", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.1, timeout))
        self._thread = None

    def _run(self) -> None:
        self.sample()
        while not self._stop.wait(self.poll_interval_seconds):
            self.sample()


def snapshot_json(snapshot: ResourceSnapshot) -> str:
    """Serialize a snapshot for tool output without inventing missing values."""
    return json.dumps(snapshot.to_dict(), ensure_ascii=False, sort_keys=True)


__all__ = ["ResourceMonitor", "ResourceSnapshot", "snapshot_json"]
