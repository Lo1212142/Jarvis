from __future__ import annotations

from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from openjarvis.server.app import create_app


class _Engine:
    pass


def _response(payload: dict) -> httpx.Response:
    return httpx.Response(200, json=payload, request=httpx.Request("GET", "https://api.open-meteo.com/v1/forecast"))


def test_cairo_weather_api_has_source_time_and_cache(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENJARVIS_HOME", str(tmp_path / "openjarvis"))
    app = create_app(_Engine(), "test", engine_name="test", api_key="test-key")
    payload = {"timezone": "Africa/Cairo", "current": {"time": "2026-08-27T12:00", "temperature_2m": 31.2, "relative_humidity_2m": 40}}
    headers = {"Authorization": "Bearer test-key"}
    with patch("openjarvis.monitoring.weather.check_ssrf", return_value=None), patch("openjarvis.monitoring.weather.httpx.get", return_value=_response(payload)) as fetch:
        with TestClient(app) as client:
            result = client.get("/api/weather/cairo", headers=headers)
            assert result.status_code == 200, result.text
            weather = result.json()["weather"]
            assert weather["location"] == "Cairo, Egypt"
            assert weather["timezone"] == "Africa/Cairo"
            assert weather["current"]["temperature_2m"] == 31.2
            assert weather["source_url"].startswith("https://api.open-meteo.com/")
            assert weather["retrieved_at"]
            assert fetch.call_count == 1
            cached = client.get("/api/weather/cairo", headers=headers)
            assert cached.status_code == 200
            assert fetch.call_count == 1
            refreshed = client.get("/api/weather/cairo?refresh=true", headers=headers)
            assert refreshed.status_code == 200
            assert fetch.call_count == 2


def test_cairo_weather_api_reports_unavailable_without_fake_data(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENJARVIS_HOME", str(tmp_path / "openjarvis"))
    app = create_app(_Engine(), "test", engine_name="test", api_key="test-key")
    headers = {"Authorization": "Bearer test-key"}
    with patch("openjarvis.monitoring.weather.check_ssrf", return_value=None), patch("openjarvis.monitoring.weather.httpx.get", side_effect=httpx.ConnectError("offline")):
        with TestClient(app) as client:
            response = client.get("/api/weather/cairo", headers=headers)
            assert response.status_code == 503
            assert "unavailable" in response.json()["detail"].lower()
            assert client.get("/api/weather/cairo").status_code == 401
