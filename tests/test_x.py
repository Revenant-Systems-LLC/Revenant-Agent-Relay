from unittest.mock import MagicMock

import pytest

from src.platforms import x
from src.platforms.x import XAdapter

HOME_URL = "https://x.com/home"
OWN_POST = "/user/status/1850000000000000001"  # RR_X_USERNAME is "user"
OTHER_POST = "/someone/status/1840000000000000001"
STATUS_LINK = 'a[href*="/status/"]'


class FakeLocator:
    def __init__(self, elements=()):
        self.elements = list(elements)  # dicts: {"href": ..., "children": {selector: [...]}}

    @property
    def first(self):
        return FakeLocator(self.elements[:1])

    def count(self):
        return len(self.elements)

    def get_attribute(self, name):
        return self.elements[0].get(name) if self.elements else None

    def locator(self, selector):
        found = []
        for element in self.elements:
            found.extend(element.get("children", {}).get(selector, []))
        return FakeLocator(found)


class FakePage:
    """X after Post was clicked.

    timeline_links are status links already on the page (other people's
    posts); toast is X's "Your post was sent" toast, or None if it never
    appeared.
    """

    def __init__(self, html="", timeline_links=(), toast=None):
        self.url = HOME_URL
        self.html = html
        self.timeline_links = timeline_links
        self.toast = toast
        self.closed = False
        self.screenshots = []

    def locator(self, selector):
        if "Your post was sent" in selector:
            return FakeLocator([self.toast] if self.toast is not None else [])
        if selector == STATUS_LINK:
            return FakeLocator([{"href": href} for href in self.timeline_links])
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


@pytest.mark.parametrize("html, error_type", [
    ("<main>Home</main>", "PLATFORM_AUTOMATION_ERROR"),
    ("<div role='alert'>Action blocked. You can't post right now.</div>", "RATE_LIMIT_ERROR"),
])
def test_other_posts_on_the_page_are_not_post_confirmation(monkeypatch, tmp_path, html, error_type):
    # The composer closes onto the home timeline, so other people's status
    # links are on the page whether or not the post went out.
    page = FakePage(html, timeline_links=[OTHER_POST])

    result = _post(monkeypatch, tmp_path, page)

    assert result["success"] is False
    assert result["error_type"] == error_type


def test_send_toast_link_is_the_post_url(monkeypatch, tmp_path):
    toast = {"children": {STATUS_LINK: [{"href": OWN_POST}]}}
    page = FakePage(timeline_links=[OTHER_POST], toast=toast)

    result = _post(monkeypatch, tmp_path, page)

    assert result["success"] is True
    assert result["post_url"] == f"https://x.com{OWN_POST}"


def test_send_toast_without_link_still_confirms(monkeypatch, tmp_path):
    page = FakePage(timeline_links=[OTHER_POST], toast={})

    result = _post(monkeypatch, tmp_path, page)

    assert result["success"] is True
    assert result["post_url"] is None


def test_navigating_to_the_accounts_own_post_after_submit_confirms(monkeypatch, tmp_path):
    page = FakePage()

    def submit(*args):
        page.url = f"https://x.com{OWN_POST}"

    result = _post(monkeypatch, tmp_path, page, _submit_post=submit)

    assert result["success"] is True
    assert result["post_url"] == f"https://x.com{OWN_POST}"


def test_navigating_to_someone_elses_post_is_not_confirmation(monkeypatch, tmp_path):
    # A click that opened a post in the timeline instead of sending ours.
    page = FakePage()

    def submit(*args):
        page.url = f"https://x.com{OTHER_POST}"

    result = _post(monkeypatch, tmp_path, page, _submit_post=submit)

    assert result["success"] is False
