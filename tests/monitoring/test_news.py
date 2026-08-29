from unittest.mock import patch

import httpx
import pytest

from openjarvis.monitoring.news import NewsMonitor


_RSS = b"""<rss><channel><item><title>Jarvis release</title><link>https://news.example/item-1</link><guid>item-1</guid><description>New feature</description></item></channel></rss>"""


def test_news_monitor_parses_and_deduplicates_allowlisted_feed():
    monitor = NewsMonitor(["https://news.example/feed.xml"])
    response = httpx.Response(200, content=_RSS, request=httpx.Request("GET", "https://news.example/feed.xml"))
    with patch("httpx.get", return_value=response) as get, patch("openjarvis.monitoring.news.check_ssrf", return_value=None):
        items = monitor.fetch("https://news.example/feed.xml")
        assert items[0].title == "Jarvis release"
        assert monitor.fetch("https://news.example/feed.xml") == []
        assert get.call_count == 2


def test_news_monitor_rejects_unallowlisted_and_private_feeds():
    monitor = NewsMonitor(["https://news.example/feed.xml"])
    with pytest.raises(ValueError, match="allowlist"):
        monitor.fetch("https://other.example/feed.xml")
    private = NewsMonitor(["https://127.0.0.1/feed.xml"])
    with pytest.raises(ValueError):
        private.fetch("https://127.0.0.1/feed.xml")
