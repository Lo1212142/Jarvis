from __future__ import annotations

import json
from pathlib import Path

import pytest

from openjarvis.self_development.pipeline import IntegrationRequest, prepare_integration


class _Response:
    content = b"Zoho OAuth documentation"
    headers = {"content-type": "text/html; charset=utf-8"}
    is_redirect = False
    is_redirection = False

    def raise_for_status(self) -> None:
        return None


class _Client:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None

    def get(self, url: str):
        return _Response()


def test_prepare_integration_creates_review_artifacts(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "openjarvis.self_development.pipeline.httpx.Client", _Client
    )
    monkeypatch.setattr(
        "openjarvis.self_development.pipeline.check_ssrf", lambda url: None
    )
    req = IntegrationRequest(
        request="Add read-only email search",
        provider="zoho",
        docs_url="https://www.zoho.com/mail/help/api/",
        requested_capabilities=("read", "search"),
    )
    artifact = prepare_integration(req, base_dir=tmp_path)
    assert artifact.status == "prepared"
    assert artifact.activation == "blocked_pending_review"
    assert Path(artifact.workspace).parent == tmp_path
    manifest = json.loads(Path(artifact.manifest).read_text())
    assert manifest["production_tree_modified"] is False
    assert Path(artifact.plan).read_text().find("OAuth") >= 0


def test_prepare_integration_rejects_non_https_or_unknown_docs() -> None:
    with pytest.raises(ValueError):
        prepare_integration(
            IntegrationRequest(
                request="Build tool",
                provider="example",
                docs_url="http://example.com/docs",
            ),
            base_dir="/tmp/openjarvis-test-self-development",
        )
