import json
from pathlib import Path

from fastapi.testclient import TestClient

from openjarvis.server.app import create_app


class _Engine:
    engine_id = "test"

    def health(self):
        return True


def test_log_api_register_search_tail_incident(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENJARVIS_LOG_ROOT", str(tmp_path))
    root = tmp_path / "openjarvis-logs"
    root.mkdir()
    (root / "service.log").write_text("INFO started token=secret\nERROR request timed out\n", encoding="utf-8")
    app = create_app(_Engine(), "test", engine_name="test", api_key="test-key")
    headers = {"Authorization": "Bearer test-key"}
    with TestClient(app) as client:
        assert client.post("/api/logs/sources", headers=headers, json={"name": "service", "relative_path": "service.log"}).status_code == 200
        search = client.get("/api/logs/service/search", headers=headers, params={"contains": "secret"})
        assert search.status_code == 200
        assert "[REDACTED]" in search.json()["entries"][0]["text"]
        tail = client.get("/api/logs/service/tail", headers=headers)
        assert tail.status_code == 200
        assert "[REDACTED]" in "\n".join(tail.json()["lines"])
        incident = client.get("/api/logs/service/incident", headers=headers)
        assert incident.json()["error_count"] == 1
        assert client.get("/api/logs/service/search").status_code == 401


def test_log_api_rejects_path_escape(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENJARVIS_LOG_ROOT", str(tmp_path))
    app = create_app(_Engine(), "test", engine_name="test", api_key="test-key")
    with TestClient(app) as client:
        response = client.post("/api/logs/sources", headers={"Authorization": "Bearer test-key"}, json={"name": "escape", "relative_path": "../outside.log"})
        assert response.status_code == 422


def test_paired_device_log_scope_is_read_only_and_hides_paths(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENJARVIS_LOG_ROOT", str(tmp_path))
    root = tmp_path / "openjarvis-logs"
    root.mkdir()
    (root / "service.log").write_text("INFO started token=secret\n", encoding="utf-8")
    app = create_app(_Engine(), "test", engine_name="test", api_key="test-key")
    global_headers = {"Authorization": "Bearer test-key"}
    pairing = app.state.pairing_service
    created = pairing.create(server_url="https://jarvis.example", scopes={"logs"})
    payload = json.loads(pairing.qr_data_for(created["pairing_id"]))
    consumed = pairing.consume(pairing_id=payload["pairing_id"], token=payload["token"], device_name="Jarivs", scopes={"logs"})
    device_headers = {"Authorization": f"Bearer {consumed['device_token']}"}
    with TestClient(app) as client:
        assert client.post("/api/logs/sources", headers=global_headers, json={"name": "service", "relative_path": "service.log"}).status_code == 200
        sources = client.get("/api/logs/sources", headers=device_headers)
        assert sources.status_code == 200
        assert sources.json() == {"sources": [{"name": "service"}]}
        assert client.get("/api/logs/service/search", headers=device_headers).status_code == 200
        blocked = client.post("/api/logs/sources", headers=device_headers, json={"name": "another", "relative_path": "service.log"})
        assert blocked.status_code == 401


def test_paired_device_without_logs_scope_cannot_read_log_center(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENJARVIS_LOG_ROOT", str(tmp_path))
    app = create_app(_Engine(), "test", engine_name="test", api_key="test-key")
    pairing = app.state.pairing_service
    created = pairing.create(server_url="https://jarvis.example", scopes={"read"})
    payload = json.loads(pairing.qr_data_for(created["pairing_id"]))
    consumed = pairing.consume(pairing_id=payload["pairing_id"], token=payload["token"], device_name="Jarivs", scopes={"read"})
    device_headers = {"Authorization": f"Bearer {consumed['device_token']}"}
    with TestClient(app) as client:
        assert client.get("/api/logs/sources", headers=device_headers).status_code == 401
