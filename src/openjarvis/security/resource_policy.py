"""CPU-first resource policy for always-on Jarvis services."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ResourceBudget:
    cpu_cores: float = 2.0
    memory_mb: int = 768
    max_parallel_jobs: int = 2
    idle_shutdown_seconds: int = 900
    gpu_enabled: bool = False
    sandbox_memory_mb: int = 512
    sandbox_timeout_seconds: int = 300

    @classmethod
    def from_environment(cls) -> "ResourceBudget":
        def positive_float(name: str, default: float) -> float:
            try:
                return max(0.25, float(os.environ.get(name, default)))
            except ValueError:
                return default

        def bounded_int(name: str, default: int, low: int, high: int) -> int:
            try:
                return min(high, max(low, int(os.environ.get(name, default))))
            except ValueError:
                return default

        return cls(
            cpu_cores=positive_float("JARVIS_CPU_LIMIT", 2.0),
            memory_mb=bounded_int("JARVIS_MEMORY_MB", 768, 256, 8192),
            max_parallel_jobs=bounded_int("JARVIS_MAX_PARALLEL_JOBS", 2, 1, 16),
            idle_shutdown_seconds=bounded_int("JARVIS_IDLE_SHUTDOWN_SECONDS", 900, 60, 86400),
            gpu_enabled=os.environ.get("JARVIS_ENABLE_GPU", "0").lower() in {"1", "true", "yes"},
            sandbox_memory_mb=bounded_int("JARVIS_SANDBOX_MEMORY_MB", 512, 64, 4096),
            sandbox_timeout_seconds=bounded_int("JARVIS_SANDBOX_TIMEOUT_SECONDS", 300, 1, 3600),
        )

    def docker_limits(self) -> dict[str, str | bool]:
        return {
            "cpus": str(self.cpu_cores),
            "memory": f"{self.memory_mb}m",
            "gpu_enabled": self.gpu_enabled,
            "no_gpu_reservation": not self.gpu_enabled,
        }


__all__ = ["ResourceBudget"]
