import base64

from openjarvis.browser.computer import BrowserComputerSession


class FakePage:
    def __init__(self, text="Welcome", url="https://example.test/"):
        self.text = text
        self.current_url = url
        self.clicked = []
        self.filled = []
        self.evaluated = []
        self.dragged = []

    def goto(self, url, *, wait_until="domcontentloaded"):
        self.current_url = url

    def title(self):
        return "Example"

    def url(self):
        return self.current_url

    def inner_text(self, selector="body"):
        return self.text

    def screenshot(self, *, type="png", full_page=False):
        return b"png-bytes"

    def click(self, selector):
        self.clicked.append(selector)

    def fill(self, selector, value):
        self.filled.append((selector, value))

    def press(self, selector, key):
        self.evaluated.append(("press", selector, key))

    def drag_and_drop(self, source, target):
        self.dragged.append((source, target))

    def hover(self, selector):
        self.evaluated.append(("hover", selector))

    def select_option(self, selector, value):
        self.evaluated.append(("select", selector, value))

    def wait_for_timeout(self, timeout):
        self.evaluated.append(("wait", timeout))

    def evaluate(self, expression, *args):
        self.evaluated.append((expression, args))


class FakeRuntime:
    def __init__(self, page):
        self.page = page
        self.closed = False

    def new_page(self):
        return self.page

    def close(self):
        self.closed = True


def test_browser_computer_natural_actions_and_bounded_screenshot():
    page = FakePage()
    session = BrowserComputerSession(runtime=FakeRuntime(page))
    assert session.start()["status"] == "ready"
    navigation = session.navigate("https://example.test/form")
    assert navigation.success is True
    assert session.act("scroll", value="900").success is True
    assert session.act("fill", selector="#name", value="Tony").success is True
    assert session.act("click", selector="#next").success is True
    assert session.act("extract", selector="body").data["text"] == "Welcome"
    image = session.screenshot()
    assert base64.b64decode(image["data_base64"]) == b"png-bytes"
    assert any(event.event_type == "action" for event in session.events())


def test_tabs_drag_and_ssrf_boundaries():
    page = FakePage()
    runtime = FakeRuntime(page)
    session = BrowserComputerSession(runtime=runtime)
    session.start()
    assert session.act("tab_list").data["tabs"][0]["index"] == 0
    assert session.act("tab_new").success is True
    assert session.act("tab_switch", value="0").success is True
    assert session.act("drag", selector="#source", value="#target").success is True
    assert session.act("hover", selector="#menu").success is True
    assert session.act("select", selector="#country", value="EG").success is True
    assert session.act("wait", value="1").success is True
    assert page.dragged == [("#source", "#target")]
    blocked = session.navigate("http://127.0.0.1:8000/private")
    assert blocked.success is False
    assert "SSRF blocked" in blocked.error


def test_captcha_stops_session_until_manual_resume():
    page = FakePage(text="Please complete the CAPTCHA to continue", url="https://example.test/challenge")
    session = BrowserComputerSession(runtime=FakeRuntime(page))
    result = session.navigate(page.current_url)
    assert result.captcha_detected is True
    assert session.status == "captcha_pending"
    blocked = session.act("click", selector="#continue")
    assert blocked.success is False
    assert "manual" in blocked.error
    assert session.resume_after_captcha() is True
    assert session.status == "ready"


def test_sensitive_actions_require_one_time_approval():
    page = FakePage()
    session = BrowserComputerSession(runtime=FakeRuntime(page))
    session.start()
    pending = session.act("click", selector="button#submit-payment")
    assert pending.approval_required is True
    assert page.clicked == []
    assert session.approve(pending.approval_id) is True
    approved = session.act("click", selector="button#submit-payment", approval_id=pending.approval_id)
    assert approved.success is True
    assert page.clicked == ["button#submit-payment"]
    reused = session.act("click", selector="button#submit-payment", approval_id=pending.approval_id)
    assert reused.approval_required is True
