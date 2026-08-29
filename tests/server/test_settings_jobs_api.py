import time

from fastapi.testclient import TestClient

from openjarvis.server.app import create_app


class _Engine:
    engine_id = "test"

    def health(self):
        return True


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENJARVIS_HOME", str(tmp_path / "openjarvis"))
    return TestClient(create_app(_Engine(), "test", engine_name="test", api_key="test-key"))


def test_settings_requires_bearer_and_persists_safe_values(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        assert client.get("/api/settings").status_code == 401
        headers = {"Authorization": "Bearer test-key"}
        response = client.patch(
            "/api/settings",
            headers=headers,
            json={"nim_rpm_limit": 40, "wake_words": [" Jarvis ", "يا جارفيس", "Jarvis"]},
        )
        assert response.status_code == 200
        settings = response.json()["settings"]
        assert settings["nim_rpm_limit"] == 40
        assert settings["wake_words"] == ["Jarvis", "يا جارفيس"]
        assert response.json()["credentials"]
        assert response.json()["security"]["secrets_returned"] is False
        assert client.patch("/api/settings", headers=headers, json={"nim_rpm_limit": 41}).status_code == 422


def test_settings_configures_live_monitor_and_public_services(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENJARVIS_HOME", str(tmp_path / "openjarvis"))
    app = create_app(_Engine(), "test", engine_name="test", api_key="test-key")
    music_root = tmp_path / "music"
    music_root.mkdir()
    with TestClient(app) as client:
        response = client.patch(
            "/api/settings",
            headers={"Authorization": "Bearer test-key"},
            json={
                "resource_monitor_enabled": False,
                "resource_poll_interval_seconds": 9,
                "resource_cpu_alert_percent": 91,
                "resource_memory_alert_percent": 81,
                "resource_alert_cooldown_seconds": 45,
                "audio_playback_enabled": True,
                "audio_allowed_roots": [str(music_root)],
                "weather_enabled": False,
                "weather_cache_ttl_seconds": 120,
                "weather_stale_after_seconds": 900,
                "conflict_news_enabled": False,
                "conflict_news_cache_ttl_seconds": 120,
                "conflict_news_stale_after_seconds": 900,
                "conflict_news_max_items": 12,
                "media_catalog_enabled": False,
                "media_catalog_cache_ttl_seconds": 180,
                "media_catalog_max_items": 11,
            },
        )
        assert response.status_code == 200, response.text
        assert app.state.resource_monitor_enabled is False
        assert app.state.resource_monitor.config() == {
            "poll_interval_seconds": 9.0,
            "cpu_alert_percent": 91.0,
            "memory_alert_percent": 81.0,
            "alert_cooldown_seconds": 45.0,
        }
        assert app.state.audio_playback_enabled is True
        assert app.state.audio_service.roots() == [str(music_root.resolve())]
        assert app.state.weather_enabled is False
        assert app.state.weather_service.cache_ttl_seconds == 120.0
        assert app.state.weather_service.stale_after_seconds == 900.0
        assert app.state.conflict_news_enabled is False
        assert app.state.conflict_news_service.cache_ttl_seconds == 120.0
        assert app.state.conflict_news_service.stale_after_seconds == 900.0
        assert app.state.conflict_news_service.max_items == 12
        assert app.state.media_catalog_enabled is False
        assert app.state.media_catalog_service.cache_ttl_seconds == 180.0
        assert app.state.media_catalog_service.max_items == 11


def test_jobs_api_and_worker_are_available_with_bearer(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        headers = {"Authorization": "Bearer test-key"}
        created = client.post("/api/jobs", headers=headers, json={"kind": "health_check", "prompt": "smoke"})
        assert created.status_code == 200
        job_id = created.json()["job"]["id"]
        deadline = time.time() + 3
        final = None
        while time.time() < deadline:
            final = client.get(f"/api/jobs/{job_id}", headers=headers).json()["job"]
            if final["status"] == "completed":
                break
            time.sleep(0.02)
        assert final["status"] == "completed"
        assert final["progress"] == 1.0
        events = client.get(f"/api/jobs/{job_id}/events", headers=headers)
        assert events.status_code == 200
        event_types = [event["event_type"] for event in events.json()["events"]]
        assert event_types[0] == "queued"
        assert "running" in event_types
        assert "completed" in event_types
        assert client.get("/api/jobs", headers=headers).status_code == 200
