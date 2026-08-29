import base64

from fastapi.testclient import TestClient

from openjarvis.browser.routes import BrowserComputerManager
from openjarvis.server.app import create_app


class _Engine:
    engine_id = "test"

    def health(self):
        return True


class _Page:
    def __init__(self):
        self.current_url = "https://example.test/"
        self.text = "Example page"
        self.clicked = []
        self.filled = []

    def goto(self, url, *, wait_until="domcontentloaded"):
        self.current_url = url

    def title(self):
        return "Example"

    def url(self):
        return self.current_url

    def inner_text(self, selector="body"):
        return self.text

    def screenshot(self, *, type="png", full_page=False):
        return b"browser-png"

    def click(self, selector):
        self.clicked.append(selector)

    def fill(self, selector, value):
        self.filled.append((selector, value))

    def press(self, selector, key):
        pass

    def evaluate(self, expression, *args):
        pass


class _Runtime:
    def __init__(self, page):
        self.page = page
        self.closed = False

    def new_page(self):
        return self.page

    def close(self):
        self.closed = True


def test_browser_computer_api_journey(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENJARVIS_HOME", str(tmp_path / "openjarvis"))
    app = create_app(_Engine(), "test", engine_name="test", api_key="test-key")
    page = _Page()
    manager = BrowserComputerManager(runtime_factory=lambda session_id, headless: _Runtime(page))
    app.state.browser_computer_manager = manager
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer test-key"}
        created = client.post("/api/browser/computers/sessions", headers=headers, json={"headless": True})
        assert created.status_code == 200
        session_id = created.json()["session"]["session_id"]
        nav = client.post(f"/api/browser/computers/sessions/{session_id}/navigate", headers=headers, json={"url": "https://example.test/form"})
        assert nav.status_code == 200
        assert nav.json()["result"]["success"] is True
        fill = client.post(f"/api/browser/computers/sessions/{session_id}/actions", headers=headers, json={"action": "fill", "selector": "#name", "value": "Tony"})
        assert fill.json()["result"]["success"] is True
        pending = client.post(f"/api/browser/computers/sessions/{session_id}/actions", headers=headers, json={"action": "click", "selector": "#submit-payment"})
        approval_id = pending.json()["result"]["approval_id"]
        assert pending.json()["result"]["approval_required"] is True
        assert client.post(f"/api/browser/computers/sessions/{session_id}/approve", headers=headers, json={"approval_id": approval_id}).json()["approved"] is True
        approved = client.post(f"/api/browser/computers/sessions/{session_id}/actions", headers=headers, json={"action": "click", "selector": "#submit-payment", "approval_id": approval_id})
        assert approved.json()["result"]["success"] is True
        image = client.get(f"/api/browser/computers/sessions/{session_id}/screenshot", headers=headers)
        assert image.status_code == 200
        assert base64.b64decode(base64.b64encode(image.content)) == b"browser-png"
        events = client.get(f"/api/browser/computers/sessions/{session_id}/events", headers=headers)
        assert events.status_code == 200
        assert any(event["event_type"] == "approval_required" for event in events.json()["events"])
