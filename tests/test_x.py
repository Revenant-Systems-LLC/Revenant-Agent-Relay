from unittest.mock import MagicMock

import pytest

from src.platforms import x
from src.platforms.x import XAdapter


class FakeLocator:
    @property
    def first(self):
        return self

    def count(self):
        return 0

    def get_attribute(self, name):
        return None


class FakePage:
    """X after Post was clicked: no status link and no send toast on the page."""

    def __init__(self, html):
        self.url = "https://x.com/home"
        self.html = html
        self.closed = False
        self.screenshots = []

    def locator(self, selector):
        return FakeLocator()

    def content(self):
        return self.html

    def screenshot(self, path, **kwargs):
        if self.closed:
            raise RuntimeError("Target page, context or browser has been closed")
        self.screenshots.append(path)


def _fake_sync_playwright(page):
    class _Manager:
        def __enter__(self):
            p = MagicMock()
            p.chromium.launch.return_value.new_context.return_value.new_page.return_value = page
            return p

        def __exit__(self, *exc_info):
            page.closed = True  # sync_playwright() teardown closes every page
            return False

    return _Manager


def _post(monkeypatch, tmp_path, page, **steps):
    monkeypatch.setenv("RR_X_USERNAME", "user")
    monkeypatch.setenv("RR_X_PASSWORD", "pw")
    monkeypatch.setattr(x, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(x, "sync_playwright", _fake_sync_playwright(page))

    adapter = XAdapter({}, {"lead_scout": {"enabled": False}})
    # Login, compose, fill and submit all succeed unless a test passes its own
    # step; only what happens after them is under test.
    for step in ("_ensure_logged_in", "_open_compose", "_fill_caption", "_submit_post"):
        monkeypatch.setattr(adapter, step, steps.get(step, lambda *args: None))
    return adapter.post_ad({"id": "AD-1"}, "Caption")


@pytest.mark.parametrize("message", [
    "Action blocked. You can't post right now.",
    "You've hit the rate limit. Try again later.",
])
def test_action_block_after_submit_is_a_rate_limit_error(monkeypatch, tmp_path, message):
    result = _post(monkeypatch, tmp_path, FakePage(f"<div role='alert'>{message}</div>"))

    assert result["success"] is False
    assert result["error_type"] == "RATE_LIMIT_ERROR"


def test_missing_confirmation_stays_an_automation_error(monkeypatch, tmp_path):
    result = _post(monkeypatch, tmp_path, FakePage("<main>Home</main>"))

    assert result["success"] is False
    assert result["error_type"] == "PLATFORM_AUTOMATION_ERROR"


@pytest.mark.parametrize("error", [
    x.PlaywrightTimeoutError("Locator.click: Timeout 7000ms exceeded."),
    RuntimeError("Target crashed"),
])
def test_failure_screenshot_is_taken_before_playwright_shuts_down(monkeypatch, tmp_path, error):
    def broken_step(*args):
        raise error

    page = FakePage("<main>Home</main>")

    result = _post(monkeypatch, tmp_path, page, _submit_post=broken_step)

    assert result["success"] is False
    assert result["screenshot_path"] is not None
    assert page.screenshots == [result["screenshot_path"]]
