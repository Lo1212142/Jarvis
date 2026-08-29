"""Deterministic CEO/CTO project artifacts; no external side effects."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class ProjectArtifactBuilder:
    def __init__(self, output_root: str | Path) -> None:
        self.root = Path(output_root).expanduser().resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)

    def write(self, kind: str, title: str, data: dict[str, Any]) -> str:
        allowed = {"requirements", "adr", "roadmap", "release", "incident", "status", "risk_register"}
        if kind not in allowed:
            raise ValueError("unsupported project artifact kind")
        safe_title = "-".join("".join(char if char.isalnum() else "-" for char in title).split("-"))[:80].strip("-") or "artifact"
        target = (self.root / f"{kind}-{safe_title}.json").resolve()
        try:
            target.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("artifact path escaped output root") from exc
        payload = {"kind": kind, "title": title[:200], "data": data, "schema_version": 1}
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        return str(target)


__all__ = ["ProjectArtifactBuilder"]
