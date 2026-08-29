from __future__ import annotations

from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from openjarvis.server.app import create_app


class _Engine:
    pass


def _response(payload: dict) -> httpx.Response:
    return httpx.Response(200, json=payload, request=httpx.Request("GET", "https://api.gdeltproject.org/api/v2/doc/doc"))


def test_conflict_news_api_returns_provenance_and_dedupes(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENJARVIS_HOME", str(tmp_path / "openjarvis"))
    app = create_app(_Engine(), "test", engine_name="test", api_key="test-key")
    payload = {"articles": [{"title": "Ceasefire update", "url": "https://news.example/1", "domain": "news.example", "seendate": "20260827120000", "language": "English", "sourcecountry": "US"}, {"title": "Ceasefire update", "url": "https://news.example/1", "domain": "news.example"}]}
    headers = {"Authorization": "Bearer test-key"}
    with patch("openjarvis.monitoring.conflict_news.check_ssrf", return_value=None), patch("openjarvis.monitoring.conflict_news.httpx.get", return_value=_response(payload)) as fetch:
        with TestClient(app) as client:
            result = client.get("/api/news/conflicts", headers=headers)
            assert result.status_code == 200, result.text
            news = result.json()["news"]
            assert len(news["items"]) == 1
            assert news["items"][0]["url"] == "https://news.example/1"
            assert news["source_url"].startswith("https://api.gdeltproject.org/")
            assert news["retrieved_at"]
            assert fetch.call_count == 1
            assert client.get("/api/news/conflicts", headers=headers).status_code == 200
            assert fetch.call_count == 1


def test_conflict_news_api_never_fabricates_when_source_unavailable(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENJARVIS_HOME", str(tmp_path / "openjarvis"))
    app = create_app(_Engine(), "test", engine_name="test", api_key="test-key")
    with patch("openjarvis.monitoring.conflict_news.check_ssrf", return_value=None), patch("openjarvis.monitoring.conflict_news.httpx.get", side_effect=httpx.ConnectError("offline")):
        with TestClient(app) as client:
            response = client.get("/api/news/conflicts", headers={"Authorization": "Bearer test-key"})
            assert response.status_code == 503
            assert "unavailable" in response.json()["detail"].lower()
