"""Bounded CPU/RAM/concurrency allocator for trusted worker kinds."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ResourceLease:
    lease_id: str
    owner: str
    cpu: float
    memory_mb: int
    created_at: float


class ResourceAllocator:
    def __init__(self, *, cpu_cores: float = 1.0, memory_mb: int = 512, max_leases: int = 4) -> None:
        self.cpu_cores = max(0.1, float(cpu_cores))
        self.memory_mb = max(64, int(memory_mb))
        self.max_leases = max(1, min(int(max_leases), 128))
        self._leases: dict[str, ResourceLease] = {}
        self._lock = threading.RLock()
        self.audit: list[dict[str, Any]] = []

    def acquire(self, owner: str, *, cpu: float = 0.25, memory_mb: int = 128) -> ResourceLease | None:
        cpu = max(0.05, min(float(cpu), self.cpu_cores))
        memory_mb = max(64, min(int(memory_mb), self.memory_mb))
        with self._lock:
            used_cpu = sum(item.cpu for item in self._leases.values())
            used_memory = sum(item.memory_mb for item in self._leases.values())
            if len(self._leases) >= self.max_leases or used_cpu + cpu > self.cpu_cores or used_memory + memory_mb > self.memory_mb:
                self._record("denied", {"owner": owner[:128], "cpu": cpu, "memory_mb": memory_mb})
                return None
            lease = ResourceLease(uuid.uuid4().hex, owner[:128], cpu, memory_mb, __import__("time").time())
            self._leases[lease.lease_id] = lease
            self._record("acquired", {"lease_id": lease.lease_id, "owner": lease.owner})
            return lease

    def release(self, lease_id: str) -> bool:
        with self._lock:
            lease = self._leases.pop(lease_id, None)
            if lease is None:
                return False
            self._record("released", {"lease_id": lease_id, "owner": lease.owner})
            return True

    def usage(self) -> dict[str, Any]:
        with self._lock:
            return {
                "cpu_used": sum(item.cpu for item in self._leases.values()),
                "cpu_total": self.cpu_cores,
                "memory_used_mb": sum(item.memory_mb for item in self._leases.values()),
                "memory_total_mb": self.memory_mb,
                "active_leases": len(self._leases),
                "max_leases": self.max_leases,
            }

    def _record(self, event: str, payload: dict[str, Any]) -> None:
        self.audit.append({"event": event, **payload})
        self.audit = self.audit[-500:]


__all__ = ["ResourceAllocator", "ResourceLease"]
