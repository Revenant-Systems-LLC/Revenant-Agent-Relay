import pytest

from src.platforms.snapchat import SnapchatAdapter

COMPOSER_URL = "https://ads.snapchat.com/1a2b3c4d/campaigns/create"
REVIEW_URL = f"{COMPOSER_URL}/review"
SUCCESS_URL = f"{COMPOSER_URL}/success"
CAMPAIGN_URL = "https://ads.snapchat.com/1a2b3c4d/campaigns/7f3e9a2c-1b4d-4e8a-9c6f-2d5b8e1a4c7f"


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
    """Snapchat Ads Manager. `overlays` are the (role, text) alerts/dialogs on screen."""

    def __init__(self, url, overlays=(), buttons=None, inputs=None):
        self.url = url
        self.overlays = list(overlays)
        self.buttons = buttons or {}
        self.inputs = inputs or {}

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
        shown = [o for o in self.overlays if f'[role="{o[0]}"]:has-text("{o[1]}")' in selector]
        return FakeLocator(shown)


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
