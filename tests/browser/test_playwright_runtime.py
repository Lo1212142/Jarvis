import pytest

from openjarvis.browser.computer import BrowserComputerError, BrowserComputerSession, PlaywrightRuntime


def test_real_playwright_chromium_public_page(tmp_path):
    pytest.importorskip("playwright.sync_api")
    runtime = PlaywrightRuntime(user_data_dir=str(tmp_path / "profile"), headless=True)
    session = BrowserComputerSession(runtime=runtime)
    try:
        session.start()
        result = session.navigate("https://example.com")
        if not result.success and "Playwright" in result.error:
            pytest.skip(result.error)
        assert result.success is True, result.error
        assert "Example" in result.data["title"]
        screenshot = session.screenshot()
        assert screenshot["content_type"] == "image/png"
        assert len(screenshot["data_base64"]) > 100
    except BrowserComputerError as exc:
        pytest.skip(str(exc))
    finally:
        session.stop()
