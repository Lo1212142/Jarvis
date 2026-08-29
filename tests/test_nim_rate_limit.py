from __future__ import annotations

import threading
import time

import pytest

from openjarvis.security.nim_rate_limit import StrictWindowLimiter


def test_strict_window_never_admits_more_than_limit() -> None:
    limiter = StrictWindowLimiter(limit=3, window_seconds=0.08)
    admitted: list[float] = []
    lock = threading.Lock()

    def worker() -> None:
        limiter.acquire(timeout=1.0)
        with lock:
            admitted.append(time.monotonic())

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(admitted) == 3
    assert limiter.snapshot().used == 3


def test_timeout_does_not_consume_slot() -> None:
    limiter = StrictWindowLimiter(limit=1, window_seconds=0.2)
    limiter.acquire()

    with pytest.raises(TimeoutError):
        limiter.acquire(timeout=0.01)

    snapshot = limiter.snapshot()
    assert snapshot.used == 1
    assert snapshot.remaining == 0


def test_snapshot_reports_remaining_and_reset() -> None:
    limiter = StrictWindowLimiter(limit=40, window_seconds=60)
    snapshot = limiter.snapshot()
    assert snapshot.limit == 40
    assert snapshot.used == 0
    assert snapshot.remaining == 40
    assert snapshot.reset_after_seconds == 0
