from __future__ import annotations

import json
import stat
import time

from fastapi.testclient import TestClient

from openjarvis.server.app import create_app


class _Engine:
    pass


def test_qr_pairing_is_one_time_scoped_and_revokeable(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENJARVIS_HOME", str(tmp_path / "openjarvis"))
    app = create_app(_Engine(), "test", engine_name="test", api_key="server-secret")
    global_headers = {"Authorization": "Bearer server-secret"}
    with TestClient(app) as client:
        assert client.post("/api/pairing/create", json={}).status_code == 401
        created = client.post("/api/pairing/create", headers=global_headers, json={"server_url": "https://jarvis.example", "device_name_hint": "Boss phone"})
        assert created.status_code == 200, created.text
        payload = created.json()
        assert "qr_data" not in payload
        assert "token" not in created.text
        assert payload["device_name_hint"] == "Boss phone"
        qr = json.loads(app.state.pairing_service.qr_data_for(payload["pairing_id"]) or "{}")
        assert qr["pairing_id"] == payload["pairing_id"]
        assert qr["token"]
        assert qr["device_name_hint"] == "Boss phone"
        assert "server-secret" not in created.text
        assert "approvals" not in payload["scopes"]
        assert "logs" not in payload["scopes"]
        image = client.get(f"/api/pairing/{payload['pairing_id']}/qr.png", headers=global_headers)
        assert image.status_code == 200
        assert image.headers["content-type"] == "image/png"
        app.state.pairing_service._pairings[payload["pairing_id"]].expires_at = time.time() - 1  # noqa: SLF001 - exercise expiry boundary at API level
        assert client.get(f"/api/pairing/{payload['pairing_id']}/qr.png", headers=global_headers).status_code == 404
        renewed = client.post("/api/pairing/create", headers=global_headers, json={"server_url": "https://jarvis.example"})
        renewed_payload = renewed.json()
        qr = json.loads(app.state.pairing_service.qr_data_for(renewed_payload["pairing_id"]) or "{}")
        consumed = client.post("/api/pairing/consume", json={"pairing_id": renewed_payload["pairing_id"], "token": qr["token"], "device_name": "Boss phone"})
        assert consumed.status_code == 200, consumed.text
        device = consumed.json()
        assert device["device_token"].startswith("oj_dev_")
        assert "device_token" not in client.get("/api/pairing/devices", headers=global_headers).text
        assert client.post("/api/pairing/consume", json={"pairing_id": renewed_payload["pairing_id"], "token": qr["token"], "device_name": "second"}).status_code == 401
        device_headers = {"Authorization": f"Bearer {device['device_token']}"}
        current = client.get("/api/resources/current", headers=device_headers)
        assert current.status_code == 200, current.text
        assert client.get("/api/settings", headers=device_headers).status_code == 401
        listed = client.get("/api/pairing/devices", headers=global_headers).json()["devices"]
        assert listed[0]["device_id"] == device["device_id"]
        self_revoked = client.post("/api/pairing/self-revoke", headers=device_headers)
        assert self_revoked.status_code == 200, self_revoked.text
        assert self_revoked.json() == {"device_id": device["device_id"], "revoked": True}
        assert client.get("/api/resources/current", headers=device_headers).status_code == 401
        assert client.post("/api/pairing/self-revoke", headers=device_headers).status_code == 401
        assert client.post("/api/pairing/self-revoke", headers=global_headers).status_code == 403
        assert client.delete(f"/api/pairing/devices/{device['device_id']}", headers=global_headers).status_code == 200


def test_approval_scope_is_explicit_for_paired_devices(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENJARVIS_HOME", str(tmp_path / "openjarvis"))
    app = create_app(_Engine(), "test", engine_name="test", api_key="server-secret")
    headers = {"Authorization": "Bearer server-secret"}
    with TestClient(app) as client:
        created = client.post("/api/pairing/create", headers=headers, json={"server_url": "https://jarvis.example", "scopes": ["read", "approvals"]})
        assert created.status_code == 200
        assert created.json()["scopes"] == ["approvals", "read"]
        qr = json.loads(app.state.pairing_service.qr_data_for(created.json()["pairing_id"]) or "{}")
        consumed = client.post("/api/pairing/consume", json={"pairing_id": qr["pairing_id"], "token": qr["token"], "device_name": "approved phone", "scopes": ["approvals"]})
        assert consumed.status_code == 200
        assert consumed.json()["scopes"] == ["approvals"]


def test_qr_pairing_persists_device_hash_not_raw_token(tmp_path, monkeypatch) -> None:
    home = tmp_path / "openjarvis"
    monkeypatch.setenv("OPENJARVIS_HOME", str(home))
    app = create_app(_Engine(), "test", engine_name="test", api_key="server-secret")
    with TestClient(app) as client:
        created = client.post("/api/pairing/create", headers={"Authorization": "Bearer server-secret"}, json={})
        qr = json.loads(app.state.pairing_service.qr_data_for(created.json()["pairing_id"]) or "{}")
        consumed = client.post("/api/pairing/consume", json={"pairing_id": qr["pairing_id"], "token": qr["token"], "device_name": "phone"})
        raw_token = consumed.json()["device_token"]
        devices_file = home / "devices.json"
        assert devices_file.exists()
        assert raw_token not in devices_file.read_text()
        assert stat.S_IMODE(devices_file.stat().st_mode) == 0o600
