from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from openjarvis.server.app import create_app


class _Engine:
    engine_id = "test"

    def health(self):
        return True


_RSS = b"<rss><channel><item><title>Update</title><link>https://news.example/1</link><guid>1</guid></item></channel></rss>"


def test_news_api_allowlist_and_fetch():
    app = create_app(_Engine(), "test", engine_name="test", api_key="test-key")
    headers = {"Authorization": "Bearer test-key"}
    response = httpx.Response(200, content=_RSS, request=httpx.Request("GET", "https://news.example/feed.xml"))
    with patch.dict("os.environ", {"OPENJARVIS_NEWS_FEEDS": "https://news.example/feed.xml"}), patch("openjarvis.monitoring.news.check_ssrf", return_value=None), patch("httpx.get", return_value=response):
        with TestClient(app) as client:
            assert client.get("/api/monitor/news/feeds", headers=headers).json()["feeds"] == ["https://news.example/feed.xml"]
            fetched = client.post("/api/monitor/news/fetch", headers=headers, json={"feed_url": "https://news.example/feed.xml"})
            assert fetched.status_code == 200
            assert fetched.json()["items"][0]["title"] == "Update"
            rejected = client.post("/api/monitor/news/fetch", headers=headers, json={"feed_url": "https://other.example/feed.xml"})
            assert rejected.status_code == 422
