"""Bounded polling watcher for explicitly registered log files."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from openjarvis.monitoring.log_center import LogCenter


@dataclass(frozen=True, slots=True)
class LogDelta:
    source: str
    offset: int
    next_offset: int
    lines: list[str]
    dropped: int = 0


class LogWatcher:
    def __init__(self, center: LogCenter, *, max_lines_per_poll: int = 200, min_interval_seconds: float = 0.25) -> None:
        self.center = center
        self.max_lines_per_poll = max(1, min(max_lines_per_poll, 1000))
        self.min_interval_seconds = max(0.05, min(min_interval_seconds, 60.0))
        self._offsets: dict[str, int] = {}
        self._last_poll: dict[str, float] = {}

    def poll(self, source: str) -> LogDelta:
        now = time.monotonic()
        previous = self._last_poll.get(source, 0.0)
        if now - previous < self.min_interval_seconds:
            return LogDelta(source, self._offsets.get(source, 0), self._offsets.get(source, 0), [])
        self._last_poll[source] = now
        offset = self._offsets.get(source, 0)
        result = self.center.tail(source, offset=offset, limit=self.max_lines_per_poll)
        next_offset = int(result["next_offset"])
        # A rotated/truncated file starts over; do not retain an invalid offset.
        if next_offset < offset:
            offset = 0
            result = self.center.tail(source, offset=0, limit=self.max_lines_per_poll)
            next_offset = int(result["next_offset"])
        self._offsets[source] = next_offset
        lines = list(result["lines"])
        dropped = max(0, len(lines) - self.max_lines_per_poll)
        return LogDelta(source, offset, next_offset, lines[: self.max_lines_per_poll], dropped)

    def reset(self, source: str) -> None:
        self._offsets.pop(source, None)
        self._last_poll.pop(source, None)


__all__ = ["LogDelta", "LogWatcher"]
