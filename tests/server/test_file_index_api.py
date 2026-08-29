from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from openjarvis.media.file_index import FileIndexer
from openjarvis.server.app import create_app


class _Engine:
    engine_id = "test"

    def health(self):
        return True


def test_file_indexer_persists_and_searches(tmp_path):
    root = tmp_path / "workspace"
    indexer = FileIndexer(root)
    source = root / "notes.md"
    source.write_text("Jarvis project launch checklist", encoding="utf-8")
    record = indexer.index_file(source)
    assert record.filename == "notes.md"
    assert record.text_chars > 0
    assert indexer.search("launch")[0]["filename"] == "notes.md"
    with pytest.raises(ValueError, match="outside"):
        indexer.index_file(tmp_path / "outside.txt")
    indexer.close()
    reopened = FileIndexer(root)
    assert reopened.search("checklist")
    reopened.close()


def test_file_api_upload_search_and_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENJARVIS_FILE_ROOT", str(tmp_path))
    app = create_app(_Engine(), "test", engine_name="test", api_key="test-key")
    headers = {"Authorization": "Bearer test-key"}
    with TestClient(app) as client:
        upload = client.post("/api/files/upload", headers=headers, files={"upload": ("brief.md", b"CTO delivery plan", "text/markdown")})
        assert upload.status_code == 200, upload.text
        assert upload.json()["text_chars"] == len("CTO delivery plan")
        search = client.post("/api/files/search", headers=headers, json={"query": "delivery", "limit": 10})
        assert search.status_code == 200
        assert search.json()["results"][0]["filename"] == "brief.md"
        unauth = client.post("/api/files/search", json={"query": "delivery"})
        assert unauth.status_code == 401
