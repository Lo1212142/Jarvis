"""Authorized, local-only security audit tool.

The tool is intentionally defensive: it scans a user-selected workspace, never
attempts intrusion, never contacts a target, and redacts matched secret values.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec

_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(api[_-]?key|secret|token|password)\b\s*[:=]\s*['\"]?[^\s'\"]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
)
_DANGEROUS_DOCKER = (
    re.compile(r"(?i)docker\.sock"),
    re.compile(r"(?i)--privileged\b"),
    re.compile(r"(?i)\bnetwork_mode\s*:\s*host\b"),
    re.compile(r"(?i)\bcap_add\s*:\s*.*\bSYS_ADMIN\b"),
    re.compile(r"(?i)\bprivileged\s*:\s*true\b"),
)
_SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".mypy_cache"}
_MAX_FILES = 5000
_MAX_BYTES = 2_000_000


def _safe_root(raw: str) -> Path:
    root = Path(raw).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError("audit root must be an existing directory")
    return root


def _files(root: Path):
    count = 0
    for path in root.rglob("*"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if not path.is_file() or path.is_symlink():
            continue
        count += 1
        if count > _MAX_FILES:
            break
        yield path


def _scan_text(path: Path) -> tuple[bool, list[str]]:
    try:
        if path.stat().st_size > _MAX_BYTES:
            return False, []
        raw = path.read_bytes()
        if b"\x00" in raw:
            return False, []
        text = raw.decode("utf-8", errors="replace")
    except OSError:
        return False, []
    findings: list[str] = []
    for pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            findings.append("possible-secret-material")
            break
    if path.name.lower().startswith(("dockerfile", "compose")) or path.suffix in {".yml", ".yaml"}:
        for pattern in _DANGEROUS_DOCKER:
            if pattern.search(text):
                findings.append("dangerous-container-setting")
                break
    return True, findings


@ToolRegistry.register("security_audit")
class SecurityAuditTool(BaseTool):
    tool_id = "security_audit"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="security_audit",
            description=(
                "Run a local defensive security audit on an explicitly authorized "
                "workspace. Reports redacted findings only; never performs intrusion "
                "or network probing."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "root": {"type": "string", "description": "Authorized local directory."},
                    "include_hashes": {"type": "boolean", "description": "Include file hashes."},
                },
                "required": ["root"],
            },
            category="security",
            requires_confirmation=True,
            timeout_seconds=120.0,
            required_capabilities=["security:authorized-audit"],
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            root = _safe_root(str(params.get("root", "")))
            include_hashes = bool(params.get("include_hashes", False))
            findings: list[dict[str, Any]] = []
            scanned = 0
            for path in _files(root):
                scanned += 1
                readable, file_findings = _scan_text(path)
                try:
                    mode = path.stat().st_mode
                    if mode & 0o002:
                        file_findings.append("world-writable")
                    digest = hashlib.sha256(path.read_bytes()).hexdigest() if include_hashes else ""
                except OSError:
                    continue
                if file_findings:
                    item: dict[str, Any] = {
                        "path": str(path.relative_to(root)),
                        "findings": sorted(set(file_findings)),
                        "readable_text": readable,
                    }
                    if include_hashes:
                        item["sha256"] = digest
                    findings.append(item)
            return ToolResult(
                tool_name=self.tool_id,
                content=f"Scanned {scanned} authorized files; found {len(findings)} redacted findings.",
                success=True,
                metadata={"root": str(root), "scanned": scanned, "findings": findings, "network_access": False},
            )
        except Exception as exc:
            return ToolResult(tool_name=self.tool_id, content=f"Security audit failed: {exc}", success=False)


__all__ = ["SecurityAuditTool"]
