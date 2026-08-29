from __future__ import annotations

from fastapi.testclient import TestClient

from openjarvis.server.app import create_app


class _Engine:
    pass


def test_audio_api_requires_enablement_and_acknowledgement(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENJARVIS_HOME", str(tmp_path / "openjarvis"))
    audio_root = tmp_path / "audio"
    audio_root.mkdir()
    track = audio_root / "boss-tone.wav"
    track.write_bytes(b"RIFF" + b"audio-fixture")
    app = create_app(_Engine(), "test", engine_name="test", api_key="test-key")
    headers = {"Authorization": "Bearer test-key"}
    with TestClient(app) as client:
        assert client.post("/api/audio/clients", headers=headers, json={"client_id": "boss-client"}).status_code == 409
        configured = client.patch(
            "/api/settings",
            headers=headers,
            json={"audio_playback_enabled": True, "audio_allowed_roots": [str(audio_root)]},
        )
        assert configured.status_code == 200, configured.text
        assert client.post("/api/audio/clients", headers=headers, json={"client_id": "boss-client"}).status_code == 200
        registered = client.post("/api/audio/tracks", headers=headers, json={"path": str(track), "title": "Boss Tone"})
        assert registered.status_code == 200, registered.text
        track_id = registered.json()["track_id"]
        played = client.post("/api/audio/play", headers=headers, json={"client_id": "boss-client", "track_id": track_id})
        assert played.status_code == 200, played.text
        assert played.json()["acknowledged"] is False
        command = played.json()["command"]
        stream = client.get(command["stream_path"], headers=headers)
        assert stream.status_code == 200
        assert stream.content.startswith(b"RIFF")
        acknowledged = client.post("/api/audio/ack", headers=headers, json={"client_id": "boss-client", "sequence": command["sequence"], "state": "playing"})
        assert acknowledged.status_code == 200
        assert acknowledged.json()["state"]["acknowledged_sequence"] == command["sequence"]
        volume = client.post("/api/audio/control", headers=headers, json={"client_id": "boss-client", "action": "set_volume", "value": 35})
        assert volume.status_code == 200
        assert volume.json()["state"]["volume"] == 35
        assert client.post("/api/audio/tracks", headers=headers, json={"path": str(tmp_path / "outside.wav")}).status_code == 404


def test_audio_websocket_pushes_command_and_accepts_ack(tmp_path, monkeypatch) -> None:
    import base64

    monkeypatch.setenv("OPENJARVIS_HOME", str(tmp_path / "openjarvis"))
    audio_root = tmp_path / "audio"
    audio_root.mkdir()
    track = audio_root / "tone.mp3"
    track.write_bytes(b"ID3-audio-fixture")
    app = create_app(_Engine(), "test", engine_name="test", api_key="test-key")
    headers = {"Authorization": "Bearer test-key"}
    credential = "openjarvis.key.b64url." + base64.urlsafe_b64encode(b"test-key").decode().rstrip("=")
    with TestClient(app) as client:
        assert client.patch("/api/settings", headers=headers, json={"audio_playback_enabled": True, "audio_allowed_roots": [str(audio_root)]}).status_code == 200
        registered = client.post("/api/audio/tracks", headers=headers, json={"path": str(track)})
        track_id = registered.json()["track_id"]
        with client.websocket_connect("/api/audio/ws?client_id=ws-client", subprotocols=["openjarvis.auth.v1", credential]) as websocket:
            assert websocket.receive_json()["type"] == "audio.ready"
            played = client.post("/api/audio/play", headers=headers, json={"client_id": "ws-client", "track_id": track_id})
            assert played.status_code == 200
            command = websocket.receive_json()
            assert command["type"] == "audio.play"
            websocket.send_json({"type": "audio.ack", "sequence": command["sequence"], "state": "playing"})
            state = client.get("/api/audio/state?client_id=ws-client", headers=headers)
            assert state.json()["state"]["acknowledged_sequence"] == command["sequence"]
