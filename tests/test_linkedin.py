from unittest.mock import MagicMock

import pytest

from src.platforms import linkedin
from src.platforms.linkedin import LinkedInAdapter

FEED_URL = "https://www.linkedin.com/feed/"
OTHER_POST = "/feed/update/urn:li:activity:7000000000000000001/"
NEW_POST = "/feed/update/urn:li:activity:7999999999999999999/"
POST_LINK = 'a[href*="/feed/update/"]'


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
    """A LinkedIn page after Post was clicked.

    feed_links are post links already on the page (other people's posts);
    toast is LinkedIn's success toast, or None if it never appeared.
    """

    def __init__(self, url, html="", feed_links=(), toast=None):
        self.url = url
        self.html = html
        self.feed_links = feed_links
        self.toast = toast
        self.closed = False
        self.screenshots = []

    def content(self):
        return self.html

    def locator(self, selector):
        if "Post successful" in selector:
            return FakeLocator([self.toast] if self.toast is not None else [])
        if selector == POST_LINK:
            return FakeLocator([{"href": href} for href in self.feed_links])
        return FakeLocator()

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


def _adapter(monkeypatch, tmp_path):
    monkeypatch.setenv("RR_LINKEDIN_USERNAME", "user@example.com")
    monkeypatch.setenv("RR_LINKEDIN_PASSWORD", "pw")
    monkeypatch.setenv("RR_LINKEDIN_POST_TARGET", "profile")
    monkeypatch.setattr(linkedin, "LOGS_DIR", tmp_path / "logs")
    # Keep the lead-scout sweep out of publish confirmation.
    return LinkedInAdapter({}, {"lead_scout": {"enabled": False}})


@pytest.mark.parametrize("url", [FEED_URL, "https://www.linkedin.com/company/revenant-systems-llc/posts/"])
def test_other_posts_on_the_page_are_not_publish_confirmation(monkeypatch, tmp_path, url):
    page = FakePage(url, html="<p>Jane Doe shared this</p>", feed_links=[OTHER_POST])

    result = _adapter(monkeypatch, tmp_path)._confirm_publish(page)

    assert result["success"] is False
    assert result["error_type"] == "PLATFORM_AUTOMATION_ERROR"
    assert result["raw_error"] == "publish_confirmation_uncertain"


def test_success_toast_link_is_the_post_url(monkeypatch, tmp_path):
    toast = {"children": {POST_LINK: [{"href": NEW_POST}]}}
    page = FakePage(FEED_URL, feed_links=[OTHER_POST], toast=toast)

    result = _adapter(monkeypatch, tmp_path)._confirm_publish(page)

    assert result["success"] is True
    assert result["post_url"] == f"https://www.linkedin.com{NEW_POST}"


def test_success_toast_without_link_still_confirms(monkeypatch, tmp_path):
    page = FakePage(FEED_URL, toast={})

    result = _adapter(monkeypatch, tmp_path)._confirm_publish(page)

    assert result["success"] is True
    assert result["post_url"] is None
    assert result["raw_error"] == "explicit_success_toast_detected"


@pytest.mark.parametrize("url", [
    f"https://www.linkedin.com{NEW_POST}",
    "https://www.linkedin.com/posts/revenant_launch-activity-7999999999999999999-AbCd/",
])
def test_landing_on_the_post_permalink_confirms(monkeypatch, tmp_path, url):
    result = _adapter(monkeypatch, tmp_path)._confirm_publish(FakePage(url))

    assert result["success"] is True
    assert result["post_url"] == url


@pytest.mark.parametrize("url", [
    "https://www.linkedin.com/authwall?trk=gf&sessionRedirect=https://www.linkedin.com/feed/",
    "https://www.linkedin.com/checkpoint/challenge/AgE?original_referer=/feed/",
])
def test_feed_named_in_a_redirect_query_is_not_a_session(monkeypatch, tmp_path, url):
    assert _adapter(monkeypatch, tmp_path)._is_logged_in(FakePage(url)) is False


def test_feed_page_is_a_session(monkeypatch, tmp_path):
    assert _adapter(monkeypatch, tmp_path)._is_logged_in(FakePage(FEED_URL)) is True


def test_rate_limit_is_reported_when_nothing_confirms(monkeypatch, tmp_path):
    page = FakePage(FEED_URL, html="<div>You are temporarily restricted from posting.</div>")

    result = _adapter(monkeypatch, tmp_path)._confirm_publish(page)

    assert result["success"] is False
    assert result["error_type"] == "RATE_LIMIT_ERROR"


@pytest.mark.parametrize("error", [
    linkedin.PlaywrightTimeoutError("Locator.click: Timeout 5000ms exceeded."),
    RuntimeError("Target crashed"),
])
def test_failure_screenshot_is_taken_before_playwright_shuts_down(monkeypatch, tmp_path, error):
    page = FakePage(FEED_URL)
    adapter = _adapter(monkeypatch, tmp_path)
    monkeypatch.setattr(linkedin, "sync_playwright", _fake_sync_playwright(page))

    def broken_step(*args):
        raise error

    monkeypatch.setattr(adapter, "_ensure_authenticated", broken_step)

    result = adapter.post_ad({"id": "AD-1"}, "Caption")

    assert result["success"] is False
    assert result["screenshot_path"] is not None
    assert page.screenshots == [result["screenshot_path"]]
