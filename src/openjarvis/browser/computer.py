"""Server-side browser computer primitives with explicit safety gates.

The browser is treated as an untrusted execution surface. This module keeps a
session in an isolated profile, exposes bounded actions, detects CAPTCHA pages,
and requires approval before high-impact actions. It does not bypass CAPTCHA.
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import urlparse


class BrowserPage(Protocol):
    def goto(self, url: str, *, wait_until: str = "domcontentloaded") -> Any: ...
    def title(self) -> str: ...
    def url(self) -> str: ...
    def inner_text(self, selector: str = "body") -> str: ...
    def screenshot(self, *, type: str = "png", full_page: bool = False) -> bytes: ...
    def click(self, selector: str) -> Any: ...
    def fill(self, selector: str, value: str) -> Any: ...
    def press(self, selector: str, key: str) -> Any: ...
    def drag_and_drop(self, source: str, target: str) -> Any: ...
    def hover(self, selector: str) -> Any: ...
    def select_option(self, selector: str, value: str) -> Any: ...
    def wait_for_timeout(self, timeout: float) -> Any: ...
    def mouse_click(self, x: float, y: float, button: str = "left") -> Any: ...
    def mouse_move(self, x: float, y: float) -> Any: ...
    def key_press(self, key: str) -> Any: ...
    def evaluate(self, expression: str, *args: Any) -> Any: ...


class BrowserRuntime(Protocol):
    def new_page(self) -> BrowserPage: ...
    def close(self) -> None: ...


_CAPTCHA_MARKERS = (
    "captcha",
    "recaptcha",
    "hcaptcha",
    "challenge-platform",
    "verify you are human",
    "prove you are human",
)
_SIDE_EFFECT_MARKERS = (
    "submit", "send", "purchase", "buy", "pay", "delete", "remove",
    "confirm", "checkout", "login", "sign in", "authorize", "transfer",
)
_SENSITIVE_MARKERS = ("password", "passcode", "token", "secret", "api key", "credit card", "card number", "cvv")
_ALLOWED_ACTIONS = {"click", "fill", "press", "scroll", "extract", "drag", "hover", "select", "wait", "mouse_click", "mouse_move", "key_press", "tab_new", "tab_list", "tab_switch"}


@dataclass(frozen=True, slots=True)
class BrowserEvent:
    event_type: str
    payload: dict[str, Any]
    created_at: str


@dataclass(frozen=True, slots=True)
class BrowserActionResult:
    action: str
    success: bool
    approval_required: bool = False
    approval_id: str = ""
    captcha_detected: bool = False
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""


class BrowserComputerError(ValueError):
    pass


class PlaywrightRuntime:
    """Lazy Chromium runtime. Browser binaries are installed by the deployment image."""

    def __init__(self, *, user_data_dir: str, headless: bool = True) -> None:
        self.user_data_dir = user_data_dir
        self.headless = headless
        self._playwright: Any = None
        self._context: Any = None

    def new_page(self) -> BrowserPage:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserComputerError("Playwright is not installed; install the browser extra") from exc
        if self._context is None:
            self._playwright = sync_playwright().start()
            launch_kwargs: dict[str, Any] = {
                "headless": self.headless,
                "accept_downloads": False,
                "java_script_enabled": True,
            }
            # Prefer an explicitly configured system browser, then Chromium on
            # PATH. This avoids downloading a second browser into the image.
            executable = os.environ.get("OPENJARVIS_CHROMIUM_PATH") or shutil.which("chromium")
            if executable:
                launch_kwargs["executable_path"] = executable
            self._context = self._playwright.chromium.launch_persistent_context(self.user_data_dir, **launch_kwargs)
        return self._context.new_page()

    def close(self) -> None:
        if self._context is not None:
            self._context.close()
        if self._playwright is not None:
            self._playwright.stop()
        self._context = None
        self._playwright = None


class BrowserComputerSession:
    def __init__(self, *, session_id: str | None = None, runtime: BrowserRuntime | None = None, headless: bool = True) -> None:
        self.session_id = session_id or uuid.uuid4().hex
        self.runtime = runtime
        self.headless = headless
        self.page: BrowserPage | None = None
        self.pages: list[BrowserPage] = []
        self.status = "created"
        self.paused = False
        self.captcha_pending = False
        self._lock = threading.RLock()
        self._events: list[BrowserEvent] = []
        self._approvals: dict[str, str] = {}
        self._approved_once: set[str] = set()

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self.page is None:
                if self.runtime is None:
                    raise BrowserComputerError("browser runtime is not configured")
                self.page = self.runtime.new_page()
                self.pages = [self.page]
            self.status = "ready"
            self._emit("session_started", {"headless": self.headless})
            return self.snapshot()

    def navigate(self, url: str) -> BrowserActionResult:
        with self._lock:
            self._guard_operable()
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                return BrowserActionResult("navigate", False, error="only absolute http(s) URLs are allowed")
            try:
                from openjarvis.security.ssrf import check_ssrf
                ssrf_error = check_ssrf(url)
            except Exception:
                ssrf_error = "SSRF checker unavailable"
            if ssrf_error:
                return BrowserActionResult("navigate", False, error=f"SSRF blocked: {ssrf_error}")
            assert self.page is not None
            try:
                self.page.goto(url, wait_until="domcontentloaded")
                captcha = self._detect_captcha()
                self.captcha_pending = captcha
                if captcha:
                    self.paused = True
                    self.status = "captcha_pending"
                    self._emit("captcha_pending", {"url": self._page_url()})
                else:
                    self.status = "ready"
                data = {"url": self._page_url(), "title": self.page.title(), "text": self._bounded_text()}
                self._emit("navigated", {"url": data["url"], "captcha": captcha})
                return BrowserActionResult("navigate", True, captcha_detected=captcha, data=data)
            except Exception as exc:
                return BrowserActionResult("navigate", False, error=f"navigation failed: {type(exc).__name__}")

    def act(self, action: str, *, selector: str = "", value: str = "", key: str = "", approval_id: str = "", x: float | None = None, y: float | None = None, button: str = "left") -> BrowserActionResult:
        with self._lock:
            if action not in _ALLOWED_ACTIONS:
                return BrowserActionResult(action, False, error="unsupported browser action")
            if action in {"tab_list"}:
                return BrowserActionResult(action, True, data={"tabs": self._tab_list()})
            if action == "tab_new":
                try:
                    assert self.runtime is not None
                    new_page = self.runtime.new_page()
                    self.pages.append(new_page)
                    self.page = new_page
                    self._emit("tab_created", {"index": len(self.pages) - 1})
                    return BrowserActionResult(action, True, data={"tabs": self._tab_list()})
                except Exception as exc:
                    return BrowserActionResult(action, False, error=f"tab creation failed: {type(exc).__name__}")
            if action == "tab_switch":
                try:
                    index = int(value or selector)
                    if index < 0 or index >= len(self.pages):
                        return BrowserActionResult(action, False, error="tab index out of range")
                    self.page = self.pages[index]
                    self._emit("tab_switched", {"index": index})
                    return BrowserActionResult(action, True, data={"tab": index, "tabs": self._tab_list()})
                except (TypeError, ValueError):
                    return BrowserActionResult(action, False, error="tab_switch requires a numeric index")
            if action not in {"extract"}:
                if self.captcha_pending:
                    return BrowserActionResult(action, False, captcha_detected=True, error="CAPTCHA requires manual user takeover")
                try:
                    self._guard_operable()
                except BrowserComputerError as exc:
                    return BrowserActionResult(action, False, error=str(exc))
            selector = selector.strip()
            if action in {"click", "fill", "press"} and not selector:
                return BrowserActionResult(action, False, error="selector is required")
            risk_text = f"{selector} {value}".casefold()
            coordinate_approval = action == "mouse_click"
            if (action in {"click", "fill"} and self._needs_approval(risk_text)) or coordinate_approval:
                if not approval_id or self._approvals.get(approval_id) != action or approval_id not in self._approved_once:
                    approval = uuid.uuid4().hex
                    self._approvals[approval] = action
                    self._emit("approval_required", {"approval_id": approval, "action": action, "selector": selector})
                    return BrowserActionResult(action, False, approval_required=True, approval_id=approval)
                self._approved_once.discard(approval_id)
            assert self.page is not None
            try:
                if action == "click":
                    self.page.click(selector)
                elif action == "fill":
                    if len(value) > 10_000:
                        return BrowserActionResult(action, False, error="value exceeds 10000 characters")
                    self.page.fill(selector, value)
                elif action == "press":
                    if len(key) > 32:
                        return BrowserActionResult(action, False, error="key is too long")
                    self.page.press(selector, key)
                elif action == "scroll":
                    delta = max(-5000, min(5000, int(value or "800")))
                    mouse = getattr(self.page, "mouse", None)
                    if mouse is not None and callable(getattr(mouse, "wheel", None)):
                        mouse.wheel(0, delta)
                    else:
                        self.page.evaluate("(delta) => window.scrollBy(0, delta)", delta)
                elif action == "drag":
                    target = value.strip()
                    if not target:
                        return BrowserActionResult(action, False, error="drag requires target selector in value")
                    drag = getattr(self.page, "drag_and_drop", None)
                    if not callable(drag):
                        return BrowserActionResult(action, False, error="browser adapter does not support drag_and_drop")
                    drag(selector, target)
                elif action == "hover":
                    hover = getattr(self.page, "hover", None)
                    if not callable(hover):
                        return BrowserActionResult(action, False, error="browser adapter does not support hover")
                    hover(selector)
                elif action == "select":
                    select = getattr(self.page, "select_option", None)
                    if not callable(select) or not value.strip():
                        return BrowserActionResult(action, False, error="select requires an option value")
                    select(selector, value.strip())
                elif action == "wait":
                    wait_ms = max(0, min(10_000, int(value or "500")))
                    wait = getattr(self.page, "wait_for_timeout", None)
                    if callable(wait):
                        wait(wait_ms)
                elif action == "mouse_click":
                    if x is None or y is None or not (0 <= x <= 10000 and 0 <= y <= 10000):
                        return BrowserActionResult(action, False, error="mouse_click requires bounded x and y")
                    mouse = getattr(self.page, "mouse", None)
                    if mouse is None or not callable(getattr(mouse, "click", None)):
                        return BrowserActionResult(action, False, error="browser adapter does not support mouse click")
                    mouse.click(x, y, button=button if button in {"left", "right", "middle"} else "left")
                elif action == "mouse_move":
                    if x is None or y is None or not (0 <= x <= 10000 and 0 <= y <= 10000):
                        return BrowserActionResult(action, False, error="mouse_move requires bounded x and y")
                    mouse = getattr(self.page, "mouse", None)
                    if mouse is None or not callable(getattr(mouse, "move", None)):
                        return BrowserActionResult(action, False, error="browser adapter does not support mouse move")
                    mouse.move(x, y)
                elif action == "key_press":
                    if len(key) > 32 or not key:
                        return BrowserActionResult(action, False, error="key_press requires a bounded key")
                    keyboard = getattr(self.page, "keyboard", None)
                    if keyboard is None or not callable(getattr(keyboard, "press", None)):
                        return BrowserActionResult(action, False, error="browser adapter does not support keyboard")
                    keyboard.press(key)
                elif action == "extract":
                    return BrowserActionResult(action, True, data={"text": self._bounded_text(selector or "body")})
                captcha = self._detect_captcha()
                if captcha:
                    self.captcha_pending = True
                    self.paused = True
                    self.status = "captcha_pending"
                    self._emit("captcha_pending", {"url": self._page_url()})
                self._emit("action", {"action": action, "selector": selector, "captcha": captcha})
                return BrowserActionResult(action, True, captcha_detected=captcha, data={"url": self._page_url()})
            except Exception as exc:
                return BrowserActionResult(action, False, error=f"browser action failed: {type(exc).__name__}")

    def manual_act(self, action: str, *, key: str = "", x: float | None = None, y: float | None = None, button: str = "left") -> BrowserActionResult:
        """Perform a narrowly scoped user-takeover action during CAPTCHA pause."""
        with self._lock:
            if action not in {"mouse_click", "mouse_move", "key_press"}:
                return BrowserActionResult(action, False, captcha_detected=self.captcha_pending, error="manual takeover only permits mouse and keyboard input")
            if self.page is None or self.status == "stopped":
                return BrowserActionResult(action, False, error="browser session is not running")
            try:
                if action in {"mouse_click", "mouse_move"}:
                    if x is None or y is None or not (0 <= x <= 10000 and 0 <= y <= 10000):
                        return BrowserActionResult(action, False, captcha_detected=self.captcha_pending, error="bounded x and y are required")
                    mouse = getattr(self.page, "mouse", None)
                    method = getattr(mouse, "click" if action == "mouse_click" else "move", None) if mouse is not None else None
                    if not callable(method):
                        return BrowserActionResult(action, False, captcha_detected=self.captcha_pending, error="browser adapter does not support manual mouse input")
                    if action == "mouse_click":
                        method(x, y, button=button if button in {"left", "right", "middle"} else "left")
                    else:
                        method(x, y)
                else:
                    if not key or len(key) > 32:
                        return BrowserActionResult(action, False, captcha_detected=self.captcha_pending, error="bounded key is required")
                    keyboard = getattr(self.page, "keyboard", None)
                    if keyboard is None or not callable(getattr(keyboard, "press", None)):
                        return BrowserActionResult(action, False, captcha_detected=self.captcha_pending, error="browser adapter does not support manual keyboard input")
                    keyboard.press(key)
                self._emit("manual_action", {"action": action, "captcha_pending": self.captcha_pending})
                return BrowserActionResult(action, True, captcha_detected=self.captcha_pending, data={"url": self._page_url()})
            except Exception as exc:
                return BrowserActionResult(action, False, captcha_detected=self.captcha_pending, error=f"manual browser action failed: {type(exc).__name__}")

    def approve(self, approval_id: str) -> bool:
        with self._lock:
            if approval_id not in self._approvals:
                return False
            self._approved_once.add(approval_id)
            self._emit("approval_granted", {"approval_id": approval_id})
            return True

    def resume_after_captcha(self) -> bool:
        with self._lock:
            if not self.captcha_pending:
                return False
            self.captcha_pending = False
            self.paused = False
            self.status = "ready"
            self._emit("captcha_cleared_manually", {})
            return True

    def pause(self) -> None:
        with self._lock:
            self.paused = True
            self.status = "paused"
            self._emit("paused", {})

    def stop(self) -> None:
        with self._lock:
            self.status = "stopped"
            if self.runtime is not None:
                self.runtime.close()
            self.page = None
            self._emit("stopped", {})

    def screenshot(self) -> dict[str, Any]:
        with self._lock:
            self._guard_operable()
            assert self.page is not None
            image = self.page.screenshot(type="png", full_page=False)
            if len(image) > 8 * 1024 * 1024:
                raise BrowserComputerError("screenshot exceeds 8MB limit")
            return {"content_type": "image/png", "data_base64": base64.b64encode(image).decode("ascii"), "url": self._page_url()}

    def events(self, limit: int = 100) -> list[BrowserEvent]:
        with self._lock:
            return self._events[-max(1, min(int(limit), 500)):]

    def snapshot(self) -> dict[str, Any]:
        return {"session_id": self.session_id, "status": self.status, "paused": self.paused, "captcha_pending": self.captcha_pending, "url": self._page_url() if self.page else ""}

    def _page_url(self) -> str:
        assert self.page is not None
        value = getattr(self.page, "url", "")
        return str(value() if callable(value) else value)

    def _guard_operable(self) -> None:
        if self.status in {"stopped", "created"} or self.page is None:
            if self.status == "created":
                self.start()
            else:
                raise BrowserComputerError("browser session is stopped")
        if self.paused:
            raise BrowserComputerError("browser session is paused")

    def _tab_list(self) -> list[dict[str, Any]]:
        tabs = []
        for index, page in enumerate(self.pages):
            try:
                tabs.append({"index": index, "url": self._page_url_for(page), "title": page.title()})
            except Exception:
                tabs.append({"index": index, "url": "", "title": ""})
        return tabs

    @staticmethod
    def _page_url_for(page: BrowserPage) -> str:
        value = getattr(page, "url", "")
        return str(value() if callable(value) else value)

    def _bounded_text(self, selector: str = "body") -> str:
        assert self.page is not None
        text = self.page.inner_text(selector)
        return text[:50_000] + ("\n[truncated]" if len(text) > 50_000 else "")

    def _detect_captcha(self) -> bool:
        assert self.page is not None
        haystack = f"{self._page_url()}\n{self.page.title()}\n{self._bounded_text()}".casefold()
        return any(marker in haystack for marker in _CAPTCHA_MARKERS)

    @staticmethod
    def _needs_approval(text: str) -> bool:
        return any(marker in text for marker in _SIDE_EFFECT_MARKERS + _SENSITIVE_MARKERS)

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        self._events.append(BrowserEvent(event_type, dict(payload), datetime.now(timezone.utc).isoformat()))
        if len(self._events) > 500:
            del self._events[:-500]


__all__ = ["BrowserActionResult", "BrowserComputerError", "BrowserComputerSession", "BrowserEvent", "PlaywrightRuntime"]
