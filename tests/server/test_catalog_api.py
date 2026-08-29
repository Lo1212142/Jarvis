from __future__ import annotations

from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from openjarvis.server.app import create_app


class _Engine:
    pass


def _response(payload: object, url: str = "https://api.tvmaze.com/search/shows?q=jarvis") -> httpx.Response:
    return httpx.Response(200, json=payload, request=httpx.Request("GET", url))


def test_series_catalog_search_is_spoiler_safe_by_default_and_has_summary_on_request(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENJARVIS_HOME", str(tmp_path / "openjarvis"))
    app = create_app(_Engine(), "test", engine_name="test", api_key="test-key")
    payload = [{"score": 10, "show": {"id": 9, "name": "Jarvis", "summary": "<p>A helpful assistant.</p>", "premiered": "2020-01-01", "genres": ["Drama"], "rating": {"average": 8.1}, "runtime": 45, "url": "https://www.tvmaze.com/shows/9/jarvis"}}]
    headers = {"Authorization": "Bearer test-key"}
    with patch("openjarvis.media.catalog.check_ssrf", return_value=None), patch("openjarvis.media.catalog.httpx.get", return_value=_response(payload)) as fetch:
        with TestClient(app) as client:
            safe = client.post("/api/catalog/search", headers=headers, json={"query": "jarvis", "media_type": "series"})
            assert safe.status_code == 200, safe.text
            assert "overview" not in safe.json()["catalog"]["items"][0]
            assert safe.json()["spoilers"] is False
            summary = client.post("/api/catalog/search", headers=headers, json={"query": "jarvis", "media_type": "series", "include_summary": True})
            assert summary.status_code == 200
            assert summary.json()["catalog"]["items"][0]["overview"] == "A helpful assistant."
            assert fetch.call_count == 1


def test_movie_catalog_requires_server_tmdb_key_without_fabricating(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENJARVIS_HOME", str(tmp_path / "openjarvis"))
    monkeypatch.delenv("TMDB_API_KEY", raising=False)
    app = create_app(_Engine(), "test", engine_name="test", api_key="test-key")
    with TestClient(app) as client:
        response = client.post("/api/catalog/search", headers={"Authorization": "Bearer test-key"}, json={"query": "Dune", "media_type": "movie"})
        assert response.status_code == 503
        assert "tmdb" in response.json()["detail"].lower()


def test_catalog_config_hides_tmdb_secret(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENJARVIS_HOME", str(tmp_path / "openjarvis"))
    monkeypatch.setenv("TMDB_API_KEY", "server-only-secret")
    app = create_app(_Engine(), "test", engine_name="test", api_key="test-key")
    with TestClient(app) as client:
        response = client.patch("/api/catalog/config", headers={"Authorization": "Bearer test-key"}, json={"max_items": 10})
        assert response.status_code == 200
        body = response.json()
        assert body["tmdb_configured"] is True
        assert "server-only-secret" not in response.text
