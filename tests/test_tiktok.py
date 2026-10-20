from unittest.mock import MagicMock

import pytest

from src.adapter_loader import load_adapter
from src.platforms import tiktok
from src.platforms.tiktok import TikTokAdapter

UPLOAD_URL = "https://www.tiktok.com/upload"


class FakeLocator:
    def __init__(self, count=0, text="", error=None):
        self._count = count
        self._text = text
        self._error = error  # raised by fill/click/set_input_files

    @property
    def first(self):
        return self

    def count(self):
        return self._count

    def inner_text(self, **kwargs):
        return self._text

    def _act(self):
        if self._error:
            raise self._error

    def fill(self, value, **kwargs):
        self._act()

    def click(self, **kwargs):
        self._act()

    def set_input_files(self, files, **kwargs):
        self._act()


class FakePage:
    """Just enough of a Playwright page for TikTokAdapter. It stays at `url`."""

    def __init__(self, url=UPLOAD_URL, html="", visible_text="", elements=None, goto_error=None):
        self.url = url
        self.html = html
        self.visible_text = visible_text
        self.elements = elements or {}
        self.goto_error = goto_error
        self.keyboard = MagicMock()
        self.closed = False
        self.screenshots = []

    def goto(self, url, **kwargs):
        if self.goto_error:
            raise self.goto_error

    def wait_for_timeout(self, ms):
        pass

    def title(self):
        return "TikTok Studio"

    def content(self):
        return self.html

    def locator(self, selector):
        if selector == "body":
            return FakeLocator(count=1, text=self.visible_text)
        return self.elements.get(selector, FakeLocator())

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


def _set_credentials(monkeypatch):
    monkeypatch.setenv("RR_TIKTOK_USERNAME", "user")
    monkeypatch.setenv("RR_TIKTOK_PASSWORD", "pw")


def _post(monkeypatch, tmp_path, page):
    _set_credentials(monkeypatch)
    monkeypatch.setenv("RR_TIKTOK_ENABLE_AUTOMATION", "true")
    monkeypatch.setattr(tiktok, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(tiktok, "sync_playwright", _fake_sync_playwright(page))

    video = tmp_path / "ad.mp4"
    video.write_bytes(b"mp4")
    adapter = TikTokAdapter({}, {"mode": "scheduled"})
    return adapter.post_ad({"id": "AD-1", "media_path": str(video)}, "Caption")


def test_disabled_automation_leaves_tiktok_unavailable(monkeypatch):
    # Credentials alone must not load an adapter whose every post fails; the
    # loader needs an init error so dev runs fall back to the simulator.
    _set_credentials(monkeypatch)
    monkeypatch.delenv("RR_TIKTOK_ENABLE_AUTOMATION", raising=False)

    with pytest.raises(RuntimeError, match="RR_TIKTOK_ENABLE_AUTOMATION"):
        TikTokAdapter({}, {})
    assert load_adapter("tiktok", {"mode": "dev"}) is None


def test_enabled_automation_loads_tiktok_adapter(monkeypatch):
    _set_credentials(monkeypatch)
    monkeypatch.setenv("RR_TIKTOK_ENABLE_AUTOMATION", "true")

    assert isinstance(load_adapter("tiktok", {"mode": "dev"}), TikTokAdapter)


def test_element_timeout_is_not_reported_as_transient_network_error(monkeypatch, tmp_path):
    # The caption box exists but never accepts input: UI breakage, which a
    # same-run retry would only hide.
    stuck = tiktok.PlaywrightTimeoutError("Locator.click: Timeout 5000ms exceeded.")
    page = FakePage(
        visible_text="Upload video",
        elements={
            'input[type="file"]': FakeLocator(count=1),
            'div[contenteditable="true"][role="textbox"]': FakeLocator(count=1, error=stuck),
        },
    )

    result = _post(monkeypatch, tmp_path, page)

    assert result["success"] is False
    assert result["error_type"] == "PLATFORM_AUTOMATION_ERROR"
    assert result["transient"] is False


def _adapter(monkeypatch):
    _set_credentials(monkeypatch)
    monkeypatch.setenv("RR_TIKTOK_ENABLE_AUTOMATION", "true")
    return TikTokAdapter({}, {})


def test_challenge_check_ignores_words_in_page_source(monkeypatch):
    # Ordinary TikTok pages carry "verify" and "captcha" in their scripts.
    page = FakePage(
        html='<script>var verifyFp = "verify_abc123"; loadCaptchaSdk();</script>',
        visible_text="Upload video\nSelect video to upload",
    )

    assert _adapter(monkeypatch)._login_if_needed(page) is None


def test_challenge_check_flags_visible_captcha_prompt(monkeypatch):
    page = FakePage(visible_text="Verify to continue:\nDrag the slider to fit the puzzle")

    result = _adapter(monkeypatch)._login_if_needed(page)

    assert result["error_type"] == "AUTH_ERROR"
    assert result["raw"] == "auth_challenge_detected"


def test_page_load_timeout_stays_a_transient_network_error(monkeypatch, tmp_path):
    page = FakePage(goto_error=tiktok.PlaywrightTimeoutError("Page.goto: Timeout 60000ms exceeded."))

    result = _post(monkeypatch, tmp_path, page)

    assert result["success"] is False
    assert result["error_type"] == "NETWORK_ERROR"
    assert result["transient"] is True


@pytest.mark.parametrize("error", [
    tiktok.PlaywrightTimeoutError("Locator.click: Timeout 5000ms exceeded."),
    RuntimeError("Target crashed"),
])
def test_failure_screenshot_is_taken_before_playwright_shuts_down(monkeypatch, tmp_path, error):
    page = FakePage(
        visible_text="Upload video",
        elements={
            'input[type="file"]': FakeLocator(count=1),
            'div[contenteditable="true"][role="textbox"]': FakeLocator(count=1, error=error),
        },
    )

    result = _post(monkeypatch, tmp_path, page)

    assert result["success"] is False
    assert result["screenshot_path"] is not None
    assert page.screenshots == [result["screenshot_path"]]


@pytest.mark.parametrize("url, html", [
    # The two old success signals. This flow never clicks Post, so neither
    # can mean that our video was published.
    (UPLOAD_URL, '<p>You posted this</p><a href="/@someone/video/7300000000000000000">'),
    ("https://www.tiktok.com/@someone/video/7300000000000000000", ""),
])
def test_prepared_upload_is_never_reported_as_a_post(monkeypatch, tmp_path, url, html):
    page = FakePage(
        url=url,
        html=html,
        visible_text="Upload video",
        elements={
            'input[type="file"]': FakeLocator(count=1),
            'div[contenteditable="true"][role="textbox"]': FakeLocator(count=1),
        },
    )

    result = _post(monkeypatch, tmp_path, page)

    assert result["success"] is False
    assert result["error_type"] == "UPLOAD_PREP_ONLY"
    assert result["post_url"] is None
    assert result["transient"] is False
