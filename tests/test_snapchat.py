from unittest.mock import MagicMock

import pytest

from src.platforms import snapchat
from src.platforms.snapchat import SnapchatAdapter

COMPOSER_URL = "https://ads.snapchat.com/1a2b3c4d/campaigns/create"
REVIEW_URL = f"{COMPOSER_URL}/review"
SUCCESS_URL = f"{COMPOSER_URL}/success"
CAMPAIGN_URL = "https://ads.snapchat.com/1a2b3c4d/campaigns/7f3e9a2c-1b4d-4e8a-9c6f-2d5b8e1a4c7f"
CAMPAIGN_LIST_URL = "https://ads.snapchat.com/1a2b3c4d/campaigns"


class FakeLocator:
    def __init__(self, items=(), on_click=None):
        self.items = list(items)
        self.on_click = on_click

    @property
    def first(self):
        return self

    def count(self):
        return len(self.items)

    def get_attribute(self, name):
        return self.items[0].get(name) if self.items else None

    def click(self, **kwargs):
        if self.on_click:
            self.on_click()

    def fill(self, value, **kwargs):
        pass

    def set_input_files(self, files, **kwargs):
        pass


class FakePage:
    """Snapchat Ads Manager.

    `overlays` are the (role, text) alerts/dialogs on screen; `links` are the
    hrefs of campaign/ad links on the page.
    """

    def __init__(self, url, overlays=(), buttons=None, inputs=None, links=()):
        self.url = url
        self.overlays = list(overlays)
        self.buttons = buttons or {}
        self.inputs = inputs or {}
        self.links = list(links)
        self.closed = False
        self.screenshots = []

    def goto(self, url, **kwargs):
        self.url = url

    def wait_for_timeout(self, ms):
        pass

    def content(self):
        return ""

    def get_by_role(self, role, name=None):
        return self.buttons.get(name, FakeLocator())

    def locator(self, selector):
        if selector in self.inputs:
            return self.inputs[selector]
        if selector.startswith("a[href*="):
            return FakeLocator([{"href": href} for href in self.links])
        shown = [o for o in self.overlays if f'[role="{o[0]}"]:has-text("{o[1]}")' in selector]
        return FakeLocator(shown)

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


def _adapter(monkeypatch):
    monkeypatch.setenv("RR_SNAPCHAT_USERNAME", "user")
    monkeypatch.setenv("RR_SNAPCHAT_PASSWORD", "pw")
    monkeypatch.delenv("RR_SNAPCHAT_POST_TARGET", raising=False)
    monkeypatch.delenv("RR_SNAPCHAT_BUSINESS_URL", raising=False)
    return SnapchatAdapter({}, {})


@pytest.mark.parametrize("url_before_submit, url_after, overlays", [
    (REVIEW_URL, REVIEW_URL, []),  # submit failed while already on the review step
    (COMPOSER_URL, REVIEW_URL, []),  # "Review and Publish" only opened the review step
    (COMPOSER_URL, COMPOSER_URL, [("dialog", "Review your campaign")]),  # pre-publish dialog
    (CAMPAIGN_URL, CAMPAIGN_URL, []),  # entity URL that was open before the click
])
def test_review_steps_and_unchanged_urls_are_not_publish_confirmation(
    monkeypatch, url_before_submit, url_after, overlays
):
    page = FakePage(url_after, overlays=overlays)

    assert _adapter(monkeypatch)._detect_ads_confirmation(page, url_before_submit) is None


@pytest.mark.parametrize("url_after, overlays, raw", [
    (CAMPAIGN_URL, [], "entity_url_with_id_detected"),
    (SUCCESS_URL, [], "confirmation_route_detected"),
    (COMPOSER_URL, [("alert", "Campaign created")], "success_toast_or_dialog_detected"),
])
def test_signals_after_submit_still_confirm(monkeypatch, url_after, overlays, raw):
    page = FakePage(url_after, overlays=overlays)

    confirmation = _adapter(monkeypatch)._detect_ads_confirmation(page, COMPOSER_URL)

    assert confirmation["raw"] == raw


@pytest.mark.parametrize("url_after, link", [
    (COMPOSER_URL, CAMPAIGN_URL),  # submit did nothing; an existing campaign is linked
    (CAMPAIGN_LIST_URL, "/1a2b3c4d/campaigns/0c1d2e3f-4a5b-6c7d-8e9f-0a1b2c3d4e5f"),  # list of old campaigns
])
def test_links_to_existing_campaigns_are_not_publish_confirmation(monkeypatch, url_after, link):
    # Ads Manager links campaigns that already exist, and every one of them
    # has an ID. Only a navigation to the new campaign's URL confirms.
    page = FakePage(url_after, links=[link])

    assert _adapter(monkeypatch)._detect_ads_confirmation(page, COMPOSER_URL) is None


def test_failed_submit_on_review_step_is_not_reported_as_success(monkeypatch):
    page = FakePage("about:blank")
    page.buttons = {
        "Create Campaign": FakeLocator([{}], on_click=lambda: setattr(page, "url", REVIEW_URL)),
        # Name matching also hits "Review and Publish"; the click changes nothing.
        "Publish": FakeLocator([{}]),
    }
    page.inputs = {'input[type="file"]': FakeLocator([{}]), "textarea": FakeLocator([{}])}

    outcome = _adapter(monkeypatch)._attempt_ads_draft_flow(page, "ad.mp4", "Caption")

    assert outcome["success"] is False
    assert outcome["error_type"] == "PLATFORM_AUTOMATION_ERROR"


@pytest.mark.parametrize("error", [
    snapchat.PlaywrightTimeoutError("Page.goto: Timeout 60000ms exceeded."),
    RuntimeError("Target crashed"),
])
def test_failure_screenshot_is_taken_before_playwright_shuts_down(monkeypatch, tmp_path, error):
    page = FakePage("about:blank")
    adapter = _adapter(monkeypatch)
    monkeypatch.setattr(snapchat, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(snapchat, "sync_playwright", _fake_sync_playwright(page))

    def broken_step(*args):
        raise error

    monkeypatch.setattr(adapter, "_open_and_auth", broken_step)
    video = tmp_path / "ad.mp4"
    video.write_bytes(b"mp4")

    result = adapter.post_ad({"id": "AD-1", "media_path": str(video)}, "Caption")

    assert result["success"] is False
    assert result["screenshot_path"] is not None
    assert page.screenshots == [result["screenshot_path"]]
