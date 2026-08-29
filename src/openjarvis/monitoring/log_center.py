"""Bounded, read-only log analysis for service and explicitly allowed projects."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SECRET_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)(api[_-]?key|token|password|secret)(\s*[=:]\s*)[^\s,;]+"),
    re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", re.S),
)
_LEVEL_RE = re.compile(r"\b(DEBUG|INFO|NOTICE|WARN(?:ING)?|ERROR|CRITICAL|FATAL)\b", re.I)


@dataclass(frozen=True, slots=True)
class LogSource:
    name: str
    path: str


class LogCenter:
    def __init__(self, allowed_root: str | Path, *, max_bytes: int = 10 * 1024 * 1024, retention_lines: int = 50_000) -> None:
        self.root = Path(allowed_root).expanduser().resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.max_bytes = max(1024, min(max_bytes, 100 * 1024 * 1024))
        self.retention_lines = max(100, min(retention_lines, 500_000))
        self._sources: dict[str, Path] = {}

    def register_source(self, name: str, path: str | Path) -> LogSource:
        clean_name = name.strip()[:80]
        if not clean_name or any(char in clean_name for char in "/\\\x00"):
            raise ValueError("invalid log source name")
        target = Path(path).expanduser().resolve()
        self._assert_inside_root(target)
        if target.is_dir():
            raise ValueError("log source must be a file")
        self._sources[clean_name] = target
        return LogSource(clean_name, str(target.relative_to(self.root)))

    def sources(self) -> list[LogSource]:
        return [LogSource(name, str(path.relative_to(self.root))) for name, path in sorted(self._sources.items())]

    def read(self, name: str, *, contains: str = "", level: str = "", limit: int = 500) -> list[dict[str, Any]]:
        path = self._path_for(name)
        lines = self._safe_lines(path)
        needle = contains.casefold()[:256]
        wanted = level.casefold()[:16]
        selected: list[dict[str, Any]] = []
        start = max(0, len(lines) - max(1, min(limit, 2000)))
        for number, line in enumerate(lines[start:], start=start + 1):
            raw_line = line.rstrip("\n")
            redacted = self.redact(raw_line)
            detected = self.level(redacted)
            if needle and needle not in raw_line.casefold():
                continue
            if wanted and detected.casefold() != wanted:
                continue
            selected.append({"line": number, "level": detected, "text": redacted})
        return selected

    def tail(self, name: str, *, offset: int = 0, limit: int = 200) -> dict[str, Any]:
        path = self._path_for(name)
        data = self._bounded_bytes(path)
        safe_offset = max(0, min(int(offset), len(data)))
        fragment = data[safe_offset:]
        lines = fragment.decode("utf-8", errors="replace").splitlines()
        bounded = lines[: max(1, min(limit, 1000))]
        return {"source": name, "offset": safe_offset, "next_offset": len(data), "lines": [self.redact(line) for line in bounded]}

    def incident(self, name: str, *, limit: int = 2000) -> dict[str, Any]:
        entries = self.read(name, limit=limit)
        errors = [entry for entry in entries if entry["level"] in {"ERROR", "CRITICAL", "FATAL"}]
        warnings = [entry for entry in entries if entry["level"] == "WARNING"]
        return {"source": name, "observed_lines": len(entries), "error_count": len(errors), "warning_count": len(warnings), "errors": errors[-100:], "warnings": warnings[-100:], "hypothesis": self._hypothesis(errors), "confidence": "low" if errors else "none"}

    def redact(self, text: str) -> str:
        clean = text[:100_000]
        for pattern in _SECRET_PATTERNS:
            if pattern.pattern.startswith("(?i)(bearer"):
                clean = pattern.sub(r"\1[REDACTED]", clean)
            elif "PRIVATE KEY" in pattern.pattern:
                clean = pattern.sub("[PRIVATE_KEY_REDACTED]", clean)
            else:
                clean = pattern.sub(r"\1\2[REDACTED]", clean)
        return clean

    @staticmethod
    def level(text: str) -> str:
        match = _LEVEL_RE.search(text)
        if not match:
            return "INFO"
        value = match.group(1).upper()
        return "WARNING" if value == "WARN" else value

    def _path_for(self, name: str) -> Path:
        if name not in self._sources:
            raise ValueError("unknown log source")
        path = self._sources[name]
        self._assert_inside_root(path)
        return path

    def _safe_lines(self, path: Path) -> list[str]:
        return self._bounded_bytes(path).decode("utf-8", errors="replace").splitlines(True)[-self.retention_lines :]

    def _bounded_bytes(self, path: Path) -> bytes:
        if not path.exists() or not path.is_file():
            raise ValueError("log source is unavailable")
        size = path.stat().st_size
        with path.open("rb") as stream:
            if size > self.max_bytes:
                stream.seek(size - self.max_bytes)
            return stream.read(self.max_bytes)

    def _assert_inside_root(self, path: Path) -> None:
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("log path is outside the allowed root") from exc

    @staticmethod
    def _hypothesis(errors: list[dict[str, Any]]) -> str:
        if not errors:
            return "No error-level entries observed in the bounded window."
        joined = " ".join(item["text"].casefold() for item in errors[-20:])
        if "out of memory" in joined or "memoryerror" in joined:
            return "Possible memory pressure; verify resource metrics before restarting."
        if "timeout" in joined or "timed out" in joined:
            return "Possible timeout or dependency latency; correlate with provider and job events."
        if "permission" in joined or "denied" in joined:
            return "Possible permission or allowlist mismatch; inspect policy before changing access."
        return "Errors observed; correlate timestamps, job IDs, and recent changes before proposing remediation."


__all__ = ["LogCenter", "LogSource"]
