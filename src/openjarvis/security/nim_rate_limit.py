"""Strict request limiting for NVIDIA NIM completion calls.

The limiter is deliberately local and conservative: every completion attempt must
reserve a slot before the HTTP request starts. Retries must call ``acquire`` again,
so they cannot bypass the configured RPM ceiling.
"""

from __future__ import annotations

import asyncio
import functools
import threading
import time
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RateLimitSnapshot:
    limit: int
    window_seconds: float
    used: int
    remaining: int
    reset_after_seconds: float


class StrictWindowLimiter:
    """A rolling-window limiter with bounded waiting.

    ``limit`` is the maximum number of admissions during ``window_seconds``.
    There is no burst allowance beyond that limit. The implementation is process
    local; deployments with multiple API workers should place the same policy in
    a shared gateway or use one worker for the NIM key.
    """

    def __init__(self, limit: int = 40, window_seconds: float = 60.0) -> None:
        if limit < 1:
            raise ValueError("limit must be positive")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self.limit = int(limit)
        self.window_seconds = float(window_seconds)
        self._timestamps: deque[float] = deque()
        self._lock = threading.Lock()
        self._async_lock = asyncio.Lock()

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._timestamps and self._timestamps[0] <= cutoff:
            self._timestamps.popleft()

    def snapshot(self) -> RateLimitSnapshot:
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            used = len(self._timestamps)
            reset = (
                max(0.0, self._timestamps[0] + self.window_seconds - now)
                if self._timestamps
                else 0.0
            )
            return RateLimitSnapshot(
                limit=self.limit,
                window_seconds=self.window_seconds,
                used=used,
                remaining=max(0, self.limit - used),
                reset_after_seconds=reset,
            )

    def acquire(self, *, timeout: float | None = None) -> RateLimitSnapshot:
        """Wait until a slot is available, then reserve it.

        A timeout raises ``TimeoutError`` without consuming a slot. This method
        never sleeps while holding the lock.
        """
        started = time.monotonic()
        while True:
            now = time.monotonic()
            with self._lock:
                self._prune(now)
                if len(self._timestamps) < self.limit:
                    self._timestamps.append(now)
                    return RateLimitSnapshot(
                        limit=self.limit,
                        window_seconds=self.window_seconds,
                        used=len(self._timestamps),
                        remaining=max(0, self.limit - len(self._timestamps)),
                        reset_after_seconds=(
                            self._timestamps[0] + self.window_seconds - now
                        ),
                    )
                wait_for = max(0.001, self._timestamps[0] + self.window_seconds - now)
            if timeout is not None:
                remaining_timeout = timeout - (time.monotonic() - started)
                if remaining_timeout <= 0:
                    raise TimeoutError("NIM rate-limit queue timeout")
                wait_for = min(wait_for, remaining_timeout)
            time.sleep(wait_for)

    async def acquire_async(self, *, timeout: float | None = None) -> RateLimitSnapshot:
        """Async counterpart; serializes admission without blocking the event loop."""
        async with self._async_lock:
            loop = asyncio.get_running_loop()
            acquire = functools.partial(self.acquire, timeout=timeout)
            future = loop.run_in_executor(None, acquire)
            return await asyncio.wait_for(future, timeout=timeout) if timeout is not None else await future


__all__ = ["RateLimitSnapshot", "StrictWindowLimiter"]
