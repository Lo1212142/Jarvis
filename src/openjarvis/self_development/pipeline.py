"""Guarded self-development pipeline for new integrations and tools.

This module intentionally implements preparation and review artifacts first. It
never writes into the production source tree, never executes generated code, and
never activates a connector without an explicit promotion step added later.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

from openjarvis.security.ssrf import check_ssrf

_ALLOWED_DOC_HOST_SUFFIXES = (
    ".zoho.com",
    ".zoho.eu",
    ".zoho.in",
    ".microsoft.com",
    ".google.com",
    ".github.com",
    ".gitlab.com",
    ".slack.com",
    ".telegram.org",
    ".notion.so",
)
_MAX_DOC_BYTES = 1_000_000
_SAFE_PROVIDER = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")


@dataclass(frozen=True, slots=True)
class IntegrationRequest:
    request: str
    provider: str
    docs_url: str
    requested_capabilities: tuple[str, ...] = ()
    target: str = "connector"


@dataclass(frozen=True, slots=True)
class DevelopmentArtifact:
    workspace: str
    manifest: str
    plan: str
    docs_snapshot: str
    status: str
    activation: str


def _validate_request(req: IntegrationRequest) -> None:
    if not req.request.strip() or len(req.request) > 4000:
        raise ValueError("request must be 1-4000 characters")
    if not _SAFE_PROVIDER.fullmatch(req.provider):
        raise ValueError("provider contains unsupported characters")
    parsed = urlparse(req.docs_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("docs_url must be an HTTPS URL")
    host = parsed.hostname.lower().rstrip(".")
    if not any(host == suffix.lstrip(".") or host.endswith(suffix) for suffix in _ALLOWED_DOC_HOST_SUFFIXES):
        raise ValueError("docs_url host is not on the documentation allowlist")
    check_ssrf(req.docs_url)


def _fetch_docs(url: str) -> tuple[str, str]:
    """Fetch a bounded documentation snapshot without following unsafe redirects."""
    with httpx.Client(
        timeout=httpx.Timeout(15.0, connect=5.0),
        follow_redirects=False,
        headers={"User-Agent": "OpenJarvis-IntegrationBuilder/1.0"},
    ) as client:
        response = client.get(url)
        if getattr(response, "is_redirect", False) or 300 <= getattr(response, "status_code", 200) < 400:
            raise ValueError("documentation redirects are disabled; provide final HTTPS URL")
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        if "text" not in content_type and "json" not in content_type:
            raise ValueError("documentation URL did not return text or JSON")
        raw = response.content[: _MAX_DOC_BYTES]
        if len(response.content) > _MAX_DOC_BYTES:
            raw += b"\n[truncated by integration builder]\n"
        digest = hashlib.sha256(raw).hexdigest()
        return raw.decode("utf-8", errors="replace"), digest


def _plan_text(req: IntegrationRequest, digest: str) -> str:
    capabilities = ", ".join(req.requested_capabilities) or "read, list, search"
    return f"""# Guarded integration plan: {req.provider}\n\n## User request\n\n{req.request}\n\n## Proposed scope\n\nThe requested target is **{req.target}**. Initial capabilities are: **{capabilities}**.\n\n## Required implementation stages\n\n1. Extract the provider's authentication method, OAuth scopes, pagination, rate limits, error model, and webhook rules from the documentation snapshot.\n2. Define a typed connector interface with least-privilege scopes and explicit redaction rules.\n3. Implement the connector only inside this workspace, never in the production source tree.\n4. Add unit tests for request construction, pagination, retry handling, malformed responses, and secret redaction.\n5. Add mocked integration tests; real account access requires a user-supplied OAuth approval and is never performed by the builder itself.\n6. Run static checks, dependency review, SSRF checks, secret scanning, and policy checks.\n7. Produce a diff and a test report. Activation remains blocked until an explicit human approval promotes the artifact.\n\n## Safety gates\n\n- No arbitrary code from the documentation is executed.\n- No credentials are copied into prompts, snapshots, logs, or memory.\n- No production files are changed during preparation.\n- No OAuth consent, email access, send/delete action, or webhook registration occurs automatically.\n- Any write/send/delete capability requires a separate approval policy.\n\nDocumentation snapshot SHA-256: `{digest}`\n"""


def prepare_integration(
    req: IntegrationRequest,
    *,
    base_dir: str | Path | None = None,
) -> DevelopmentArtifact:
    """Create an auditable preparation workspace for a new integration."""
    _validate_request(req)
    docs, digest = _fetch_docs(req.docs_url)
    root = Path(base_dir or Path.home() / ".openjarvis" / "self-development").expanduser()
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    short = hashlib.sha256(f"{req.provider}:{req.request}:{stamp}".encode()).hexdigest()[:12]
    workspace = root / f"{stamp}-{req.provider}-{short}"
    workspace.mkdir(parents=True, exist_ok=False)

    manifest = {
        "schema": 1,
        "status": "prepared",
        "activation": "blocked_pending_review",
        "created_at_utc": stamp,
        "provider": req.provider,
        "target": req.target,
        "requested_capabilities": list(req.requested_capabilities),
        "docs_url": req.docs_url,
        "docs_sha256": digest,
        "production_tree_modified": False,
    }
    manifest_path = workspace / "manifest.json"
    plan_path = workspace / "IMPLEMENTATION_PLAN.md"
    docs_path = workspace / "DOCUMENTATION_SNAPSHOT.txt"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    plan_path.write_text(_plan_text(req, digest), encoding="utf-8")
    docs_path.write_text(docs, encoding="utf-8")
    return DevelopmentArtifact(
        workspace=str(workspace),
        manifest=str(manifest_path),
        plan=str(plan_path),
        docs_snapshot=str(docs_path),
        status="prepared",
        activation="blocked_pending_review",
    )


__all__ = ["DevelopmentArtifact", "IntegrationRequest", "prepare_integration"]
