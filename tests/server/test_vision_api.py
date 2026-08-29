from __future__ import annotations

import io

from fastapi.testclient import TestClient
from PIL import Image

from openjarvis.server.app import create_app


class _VisionEngine:
    engine_id = "nim"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def health(self) -> bool:
        return True

    def generate_vision(self, **kwargs):
        self.calls.append(kwargs)
        return {"content": "I can see a blue cup on a table.", "model": kwargs["model"], "finish_reason": "stop"}


def _jpeg_bytes() -> bytes:
    image = Image.new("RGB", (80, 60), color=(20, 180, 230))
    output = io.BytesIO()
    image.save(output, format="JPEG")
    return output.getvalue()


def _client(tmp_path, monkeypatch, *, model: str = ""):
    monkeypatch.setenv("OPENJARVIS_HOME", str(tmp_path / "openjarvis"))
    if model:
        monkeypatch.setenv("NIM_VISION_MODEL", model)
    else:
        monkeypatch.delenv("NIM_VISION_MODEL", raising=False)
    engine = _VisionEngine()
    app = create_app(engine, "test", engine_name="nim", api_key="server-secret")
    return app, engine, TestClient(app)


def test_vision_is_factual_when_disabled_or_model_missing(tmp_path, monkeypatch) -> None:
    app, _engine, client = _client(tmp_path, monkeypatch)
    headers = {"Authorization": "Bearer server-secret"}
    with client:
        status = client.get("/api/vision/status", headers=headers)
        assert status.status_code == 200
        assert status.json()["available"] is False
        assert client.post("/api/vision/analyze", headers=headers, files={"frame": ("frame.jpg", _jpeg_bytes(), "image/jpeg")}).status_code == 501
        enabled = client.patch("/api/settings", headers=headers, json={"vision_enabled": True})
        assert enabled.status_code == 200
        unavailable = client.post("/api/vision/analyze", headers=headers, files={"frame": ("frame.jpg", _jpeg_bytes(), "image/jpeg")})
        assert unavailable.status_code == 503
        assert "NIM_VISION_MODEL" in unavailable.json()["detail"]
    assert app.state.vision_service.enabled is True


def test_vision_analyzes_only_bounded_valid_user_frame_without_storage(tmp_path, monkeypatch) -> None:
    app, engine, client = _client(tmp_path, monkeypatch, model="nim-vision-test")
    headers = {"Authorization": "Bearer server-secret"}
    with client:
        assert client.patch("/api/settings", headers=headers, json={"vision_enabled": True}).status_code == 200
        response = client.post(
            "/api/vision/analyze",
            headers=headers,
            files={"frame": ("camera.jpg", _jpeg_bytes(), "image/jpeg")},
            data={"question": "What can you see?"},
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["answer"] == "I can see a blue cup on a table."
        assert payload["model"] == "nim-vision-test"
        assert payload["image_retained"] is False
        assert "observed_at" in payload
        assert len(engine.calls) == 1
        assert engine.calls[0]["question"] == "What can you see?"
        assert engine.calls[0]["image_data_url"].startswith("data:image/jpeg;base64,")
        invalid = client.post("/api/vision/analyze", headers=headers, files={"frame": ("frame.gif", b"GIF89a", "image/gif")})
        assert invalid.status_code == 415
        oversized = client.post("/api/vision/analyze", headers=headers, files={"frame": ("large.jpg", b"x" * (4 * 1024 * 1024 + 1), "image/jpeg")})
        assert oversized.status_code == 413
    assert not list((tmp_path / "openjarvis").rglob("*.jpg"))
    assert app.state.vision_service.status()["available"] is True
