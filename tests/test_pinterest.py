import re
from unittest.mock import MagicMock

from src.platforms import pinterest
from src.platforms.pinterest import PinterestAdapter

HOME_URL = "https://www.pinterest.com/"


class FakeLocator:
    def __init__(self, on_click=None):
        self.on_click = on_click

    @property
    def first(self):
        return self

    def fill(self, value, **kwargs):
        pass

    def click(self, **kwargs):
        if self.on_click:
            self.on_click()

    def set_input_files(self, files, **kwargs):
        pass


class FakePage:
    """Just enough of a Playwright page to drive PinterestAdapter."""

    def __init__(self, login_lands_on=None, timeout_on=None):
        self.url = "about:blank"
        self.closed = False
        self.screenshots = []
        self.login_lands_on = login_lands_on  # None: the login never completes
        self.timeout_on = timeout_on  # URL fragment whose goto() times out

    def goto(self, url, **kwargs):
        if self.timeout_on and self.timeout_on in url:
            raise pinterest.PlaywrightTimeoutError("Timeout 60000ms exceeded.")
        self.url = url

    def locator(self, selector):
        return FakeLocator()

    def get_by_placeholder(self, text):
        return FakeLocator()

    def get_by_text(self, text, **kwargs):
        return FakeLocator()

    def get_by_role(self, role, name=None):
        if name == "Log in" and self.login_lands_on:
            return FakeLocator(on_click=lambda: setattr(self, "url", self.login_lands_on))
        return FakeLocator()

    def wait_for_url(self, url, timeout=None):
        # Like Playwright: return at once if the current URL already matches.
        if callable(url):
            matched = url(self.url)
        else:
            glob = re.escape(url).replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
            matched = re.fullmatch(glob, self.url) is not None
        if not matched:
            raise pinterest.PlaywrightTimeoutError(f"Timeout {timeout}ms exceeded.")

    def wait_for_timeout(self, ms):
        pass

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


def _run(monkeypatch, tmp_path, page):
    monkeypatch.setenv("RR_PINTEREST_USERNAME", "user@example.com")
    monkeypatch.setenv("RR_PINTEREST_PASSWORD", "pw")
    monkeypatch.delenv("RR_PINTEREST_BOARD_NAME", raising=False)
    monkeypatch.setattr(pinterest, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(pinterest, "sync_playwright", _fake_sync_playwright(page))

    image = tmp_path / "pin.png"
    image.write_bytes(b"png")
    adapter = PinterestAdapter({}, {"mode": "scheduled", "scheduled_mode": {"headless": True}})
    return adapter.post_ad({"id": "AD-1", "title": "Title", "media_path": str(image)}, "Caption")


def test_login_still_on_login_page_is_an_auth_failure(monkeypatch, tmp_path):
    # Credentials rejected: Pinterest never navigates off /login/. The login
    # wait must not be satisfied by the login page it started on.
    page = FakePage(login_lands_on=None)

    result = _run(monkeypatch, tmp_path, page)

    assert result["success"] is False
    assert result["error_type"] == "AUTH_ERROR"


def test_login_waits_until_pinterest_leaves_the_login_page(monkeypatch, tmp_path):
    page = FakePage(login_lands_on=HOME_URL)

    result = _run(monkeypatch, tmp_path, page)

    assert result["success"] is True
