from fastapi.testclient import TestClient

from openjarvis.server.app import create_app


class _Engine:
    engine_id = "test"

    def health(self):
        return True


_OBJ = """v 0 0 0\nv 1 0 0\nv 0 1 0\nv 0 0 1\nf 1 2 3\nf 1 2 4\nf 1 3 4\nf 2 3 4\n"""


def test_model3d_inspect_and_preview(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENJARVIS_MEDIA_DIR", str(tmp_path))
    app = create_app(_Engine(), "test", engine_name="test", api_key="test-key")
    headers = {"Authorization": "Bearer test-key"}
    with TestClient(app) as client:
        inspect = client.post(
            "/api/media/3d/inspect", headers=headers,
            files={"upload": ("tetra.obj", _OBJ.encode(), "text/plain")},
        )
        assert inspect.status_code == 200, inspect.text
        assert inspect.json()["vertices"] == 4
        assert inspect.json()["faces"] == 4
        preview = client.post(
            "/api/media/3d/preview", headers=headers,
            files={"upload": ("tetra.obj", _OBJ.encode(), "text/plain")},
        )
        assert preview.status_code == 200, preview.text
        assert preview.headers["content-type"] == "image/png"
        assert preview.content.startswith(b"\x89PNG")
        unauth = client.post("/api/media/3d/inspect", files={"upload": ("tetra.obj", _OBJ.encode(), "text/plain")})
        assert unauth.status_code == 401
