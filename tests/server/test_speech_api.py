from fastapi.testclient import TestClient

from openjarvis.server.app import create_app
from openjarvis.speech.tts import TTSBackend, TTSResult
from openjarvis.core.registry import TTSRegistry


class _Engine:
    engine_id = "test"

    def health(self):
        return True


class _FakeTTS(TTSBackend):
    backend_id = "test_tts_route"

    def synthesize(self, text, *, voice_id="", speed=1.0, output_format="mp3"):
        return TTSResult(audio=b"fake-audio", format=output_format, voice_id=voice_id, duration_seconds=0.2)

    def available_voices(self):
        return ["test"]

    def health(self):
        return True


class _ExplodingTTS(TTSBackend):
    backend_id = "test_tts_private_failure"

    def synthesize(self, text, *, voice_id="", speed=1.0, output_format="mp3"):
        raise RuntimeError("provider-token=do-not-return https://private-provider.invalid/trace")

    def available_voices(self):
        return []

    def health(self):
        return False


def test_speech_synthesis_route_validates_and_returns_audio(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENJARVIS_HOME", str(tmp_path / "openjarvis"))
    try:
        TTSRegistry.register_value("test_tts_route", _FakeTTS)
    except Exception:
        pass
    with TestClient(create_app(_Engine(), "test", engine_name="test", api_key="test-key")) as client:
        headers = {"Authorization": "Bearer test-key"}
        response = client.post(
            "/v1/speech/synthesize",
            headers=headers,
            json={"text": "hello", "backend": "test_tts_route", "voice_id": "test", "output_format": "wav"},
        )
        assert response.status_code == 200
        assert response.content == b"fake-audio"
        assert response.headers["content-type"] == "audio/wav"
        assert response.headers["x-speech-backend"] == "test_tts_route"
        assert client.post("/v1/speech/synthesize", headers=headers, json={"text": ""}).status_code == 422
        assert client.post("/v1/speech/synthesize", headers=headers, json={"text": "x", "speed": 9}).status_code == 422


def test_speech_synthesis_does_not_expose_provider_exception(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENJARVIS_HOME", str(tmp_path / "openjarvis"))
    try:
        TTSRegistry.register_value("test_tts_private_failure", _ExplodingTTS)
    except Exception:
        pass
    with TestClient(create_app(_Engine(), "test", engine_name="test", api_key="test-key")) as client:
        response = client.post(
            "/v1/speech/synthesize",
            headers={"Authorization": "Bearer test-key"},
            json={"text": "hello", "backend": "test_tts_private_failure"},
        )
    assert response.status_code == 502
    assert "provider-token" not in response.text
    assert "private-provider.invalid" not in response.text
    assert "unavailable" in response.json()["detail"].lower()
