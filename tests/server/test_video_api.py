import subprocess

import pytest
from fastapi.testclient import TestClient

from openjarvis.server.app import create_app


class _Engine:
    engine_id = "test"

    def health(self):
        return True


def _make_video(path):
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
            "-i", "color=c=blue:s=320x240:d=1", "-f", "lavfi", "-i",
            "anullsrc=r=8000:cl=mono", "-t", "1", "-shortest", "-y", str(path),
        ],
        check=True,
        capture_output=True,
    )


def test_video_api_probe_thumbnail_and_transcript(tmp_path, monkeypatch):
    if not __import__("shutil").which("ffmpeg"):
        pytest.skip("ffmpeg unavailable")
    monkeypatch.setenv("OPENJARVIS_MEDIA_DIR", str(tmp_path))
    app = create_app(_Engine(), "test", engine_name="test", api_key="test-key")
    video = tmp_path / "fixture.mp4"
    _make_video(video)
    headers = {"Authorization": "Bearer test-key"}
    with TestClient(app) as client:
        files = {"upload": ("fixture.mp4", video.read_bytes(), "video/mp4")}
        probe = client.post("/api/media/video/probe", headers=headers, files=files)
        assert probe.status_code == 200, probe.text
        assert probe.json()["format_name"]
        thumb = client.post(
            "/api/media/video/thumbnail", headers=headers,
            files={"upload": ("fixture.mp4", video.read_bytes(), "video/mp4")},
            data={"timestamp_seconds": "0"},
        )
        assert thumb.status_code == 200, thumb.text
        assert thumb.headers["content-type"] == "image/jpeg"
        search = client.post(
            "/api/media/video/search-transcript", headers=headers,
            json={"query": "engine", "transcript": [{"start": 2.5, "text": "Engine online"}]},
        )
        assert search.status_code == 200
        assert search.json()["hits"][0]["timestamp_seconds"] == 2.5
        unauth = client.post("/api/media/video/search-transcript", json={"query": "x", "transcript": []})
        assert unauth.status_code == 401
