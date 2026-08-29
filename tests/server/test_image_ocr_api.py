import io

from fastapi.testclient import TestClient
from PIL import Image

from openjarvis.server.app import create_app


class _Engine:
    engine_id = "test"

    def health(self):
        return True


def _png() -> bytes:
    image = Image.new("RGB", (64, 32), "white")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_image_ocr_api_auth_and_bounded_optional_backend():
    app = create_app(_Engine(), "test", engine_name="test", api_key="test-key")
    with TestClient(app) as client:
        response = client.post("/api/media/image/ocr", headers={"Authorization": "Bearer test-key"}, files={"upload": ("screen.png", _png(), "image/png")})
        assert response.status_code in {200, 501}
        if response.status_code == 200:
            assert response.json()["engine"] == "pytesseract"
        unauth = client.post("/api/media/image/ocr", files={"upload": ("screen.png", _png(), "image/png")})
        assert unauth.status_code == 401
