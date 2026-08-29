from __future__ import annotations

from fastapi.testclient import TestClient
from types import SimpleNamespace

from openjarvis.monitoring.resource_monitor import ResourceMonitor, ResourceSnapshot
from openjarvis.server.app import create_app
from openjarvis.server.routes import _take_resource_notice


class _Engine:
    def generate(self, *args, **kwargs):  # pragma: no cover - API does not infer
        raise AssertionError("inference is not expected")


class _NimLikeEngine(_Engine):
    def rate_limit_snapshot(self):
        return {"limit": 40, "used": 3, "remaining": 37, "window_seconds": 60.0, "reset_after_seconds": 12.5}


def test_resource_alert_is_language_aware_and_not_repeated() -> None:
    monitor = ResourceMonitor(cpu_alert_percent=80, memory_alert_percent=100, alert_cooldown_seconds=0)
    monitor._evaluate_alerts(ResourceSnapshot(100.0, 90.0, 128.0, 10.0, 20.0, 40.0, 1000.0, 4, 2048.0, True))  # noqa: SLF001
    state = SimpleNamespace(resource_monitor=monitor)
    arabic = _take_resource_notice(state, "استهلاكك الحالي كام؟")
    assert arabic.startswith("يا Boss")
    assert _take_resource_notice(state, "any update?") == ""
    monitor._evaluate_alerts(ResourceSnapshot(101.0, 10.0, 128.0, 10.0, 20.0, 40.0, 1000.0, 4, 2048.0, True))  # noqa: SLF001
    monitor._evaluate_alerts(ResourceSnapshot(102.0, 90.0, 128.0, 10.0, 20.0, 40.0, 1000.0, 4, 2048.0, True))  # noqa: SLF001
    english = _take_resource_notice(state, "what is the status?")
    assert english.startswith("Boss,")


def test_resource_api_reports_current_measurement_and_auth(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENJARVIS_HOME", str(tmp_path / "openjarvis"))
    app = create_app(_Engine(), "test", engine_name="test", api_key="test-key")
    headers = {"Authorization": "Bearer test-key"}
    with TestClient(app) as client:
        assert client.get("/api/resources/current").status_code == 401
        response = client.get("/api/resources/current", headers=headers)
        assert response.status_code == 200, response.text
        payload = response.json()
        assert "snapshot" in payload
        assert "measurement_available" in payload["snapshot"]
        assert payload["monitor"]["cpu_alert_percent"] <= 1000
        assert payload["nim_limiter"] is None
        assert client.get("/api/resources/history", headers=headers).status_code == 200
        assert client.get("/api/resources/alerts", headers=headers).status_code == 200


def test_resource_api_configures_thresholds_and_bounds(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENJARVIS_HOME", str(tmp_path / "openjarvis"))
    app = create_app(_Engine(), "test", engine_name="test", api_key="test-key")
    headers = {"Authorization": "Bearer test-key"}
    with TestClient(app) as client:
        response = client.patch(
            "/api/resources/config",
            headers=headers,
            json={"enabled": False, "cpu_alert_percent": 70, "memory_alert_percent": 75},
        )
        assert response.status_code == 200, response.text
        assert response.json()["monitor"]["enabled"] is False
        assert response.json()["monitor"]["cpu_alert_percent"] == 70
        assert client.patch("/api/resources/config", headers=headers, json={"memory_alert_percent": 101}).status_code == 422
        assert client.get("/api/resources/history", headers=headers, params={"limit": 241}).status_code == 422


def test_resource_api_exposes_only_real_nim_limiter_snapshot(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENJARVIS_HOME", str(tmp_path / "openjarvis"))
    app = create_app(_NimLikeEngine(), "test", engine_name="nim", api_key="test-key")
    with TestClient(app) as client:
        payload = client.get("/api/resources/current", headers={"Authorization": "Bearer test-key"}).json()
    assert payload["nim_limiter"] == {"limit": 40, "used": 3, "remaining": 37, "window_seconds": 60.0, "reset_after_seconds": 12.5}
