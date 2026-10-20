import logging
import os
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    PlaywrightTimeoutError = Exception

from ..paths import LOGS_DIR, ROOT

_ERROR_CAUSE = {
    "AUTH_ERROR": "Invalid Pinterest credentials or login challenge blocked the session.",
    "MEDIA_ERROR": "Image file missing or Pinterest rejected the media upload.",
    "CONTENT_ERROR": "Required Pin content was missing or invalid.",
    "RATE_LIMIT_ERROR": "Pinterest is temporarily throttling publishing actions.",
    "PLATFORM_AUTOMATION_ERROR": "Pinterest UI flow changed or compose controls were unavailable.",
    "NETWORK_ERROR": "Transient network failure while loading Pinterest.",
    "UNKNOWN_ERROR": "Could not classify failure reliably.",
}

_ERROR_FIX = {
    "AUTH_ERROR": "Verify RR_PINTEREST_USERNAME / RR_PINTEREST_PASSWORD and retry.",
    "MEDIA_ERROR": "Use a valid local image path and retry with a compliant image.",
    "CONTENT_ERROR": "Ensure title/caption/url fields are populated from approved ad data.",
    "RATE_LIMIT_ERROR": "Pause Pinterest attempts and retry during the next run window.",
    "PLATFORM_AUTOMATION_ERROR": "Update Pinterest selectors/workflow in the adapter.",
    "NETWORK_ERROR": "Retry once; likely transient connectivity issue.",
    "UNKNOWN_ERROR": "Inspect logs and screenshot for manual triage.",
}

_TRANSIENT_TYPES = {"NETWORK_ERROR"}


def _resolve_media(media_path):
    p = Path(media_path)
    if p.is_absolute():
        return p
    return ROOT / p


def _is_post_login_url(url):
    """True once Pinterest has navigated off its login page.

    A glob like "**pinterest.com/**" is not enough: it also matches /login/
    itself, so wait_for_url() would return before the login has finished.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    on_pinterest = host == "pinterest.com" or host.endswith(".pinterest.com")
    return on_pinterest and not parsed.path.lower().startswith("/login")


def _result(success, post_url=None, error_type=None, detected=None, raw=None, screenshot_path=None):
    return {
        "success": success,
        "error_type": error_type,
        "detected_issue": detected,
        "likely_cause": _ERROR_CAUSE.get(error_type) if error_type else None,
        "suggested_fix": _ERROR_FIX.get(error_type) if error_type else None,
        "raw_error": raw,
        "post_url": post_url,
        "screenshot_path": str(screenshot_path) if screenshot_path else None,
        "transient": error_type in _TRANSIENT_TYPES if error_type else False,
    }


class PinterestAdapter:
    def __init__(self, pinterest_config, settings):
        if not PLAYWRIGHT_AVAILABLE:
            raise RuntimeError("playwright is not installed. Run: pip install playwright")

        cfg = pinterest_config or {}
        self.username_env = cfg.get("username_env_var", "RR_PINTEREST_USERNAME")
        self.password_env = cfg.get("password_env_var", "RR_PINTEREST_PASSWORD")
        self.board_name_env = cfg.get("board_name_env_var", "RR_PINTEREST_BOARD_NAME")

        self.username = os.getenv(self.username_env, "")
        self.password = os.getenv(self.password_env, "")
        self.board_name = os.getenv(self.board_name_env, "")

        if not self.username or not self.password:
            raise RuntimeError(
                "Pinterest credentials missing. Set env vars: "
                f"{self.username_env}, {self.password_env}"
            )

        mode = (settings or {}).get("mode", "dev")
        mode_settings = (settings or {}).get(f"{mode}_mode", {})
        self.headless = mode_settings.get("headless", mode == "scheduled")
        self.dev_mode = mode == "dev"

    def post_ad(self, ad, caption):
        media_files = []
        paths = ad.get("media_paths", [])
        if ad.get("media_path"):
            paths.insert(0, ad["media_path"])
            
        for path_str in paths:
            m_file = _resolve_media(path_str)
            if not m_file.exists() or not m_file.is_file():
                return _result(
                    False,
                    error_type="MEDIA_ERROR",
                    detected=f"Media file not found: {path_str}",
                    raw=str(m_file),
                )
            media_files.append(str(m_file))
            
        if not media_files:
            return _result(
                False,
                error_type="MEDIA_ERROR",
                detected="No media files provided for Pin",
                raw="missing_media_path",
            )

        title = (ad.get("title") or caption or "").strip()
        description = (caption or "").strip()
        destination_url = (ad.get("url") or "").strip()

        if not title:
            return _result(False, error_type="CONTENT_ERROR", detected="Missing title/caption for Pin", raw="missing_title")

        screenshot_path = None
        browser = None
        context = None
        page = None
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=self.headless)
                context = browser.new_context()
                page = context.new_page()

                if self.dev_mode:
                    print("[PinterestAdapter] Running in dev mode with visible browser.")

                self._login(page)
                post_url = self._create_pin(page, media_files[0], title, description, destination_url)

                return _result(True, post_url=post_url)
        except Exception as e:
            kind = self._classify_error(e)
            screenshot_path = self._save_failure_screenshot(page)
            return _result(False, error_type=kind, detected=str(e), raw=repr(e), screenshot_path=screenshot_path)
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    logger.debug("Failed to close browser context during cleanup", exc_info=True)
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    logger.debug("Failed to close browser during cleanup", exc_info=True)

    def _login(self, page):
        page.goto("https://www.pinterest.com/login/", wait_until="domcontentloaded", timeout=60000)
        page.locator('input[name="id"]').first.fill(self.username)
        page.locator('input[name="password"]').first.fill(self.password)
        page.get_by_role("button", name="Log in").first.click()
        try:
            page.wait_for_url(_is_post_login_url, timeout=45000)
        except PlaywrightTimeoutError as exc:
            raise RuntimeError(f"Pinterest login did not complete; still at {page.url}") from exc

    def _create_pin(self, page, media_file, title, description, destination_url):
        page.goto("https://www.pinterest.com/pin-creation-tool/", wait_until="domcontentloaded", timeout=60000)
        page.locator('input[type="file"]').first.set_input_files(media_file)

        page.get_by_placeholder("Add your title").first.fill(title[:100])
        if description:
            page.get_by_placeholder("Tell everyone what your Pin is about").first.fill(description[:500])
        if destination_url:
            page.get_by_placeholder("Add a destination link").first.fill(destination_url)

        if self.board_name:
            page.get_by_text("Choose board", exact=False).first.click()
            page.get_by_role("textbox").first.fill(self.board_name)
            page.get_by_text(self.board_name, exact=False).first.click()

        page.get_by_role("button", name="Publish").first.click()
        page.wait_for_timeout(5000)
        return "https://www.pinterest.com/"

    def _classify_error(self, exc):
        msg = str(exc).lower()
        if isinstance(exc, PlaywrightTimeoutError):
            return "PLATFORM_AUTOMATION_ERROR"
        if "login" in msg or "password" in msg or "unauthorized" in msg:
            return "AUTH_ERROR"
        if "429" in msg or "rate" in msg or "too many" in msg:
            return "RATE_LIMIT_ERROR"
        if "net::" in msg or "timed out" in msg or "connection" in msg:
            return "NETWORK_ERROR"
        if "file" in msg or "media" in msg or "upload" in msg:
            return "MEDIA_ERROR"
        if "title" in msg or "destination" in msg or "content" in msg:
            return "CONTENT_ERROR"
        return "UNKNOWN_ERROR"

    def _save_failure_screenshot(self, page):
        if page is None:
            return None
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out_dir = LOGS_DIR / datetime.now().date().isoformat() / "failures"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"pinterest-{stamp}.png"
        try:
            page.screenshot(path=str(path), full_page=True)
            return path
        except Exception:
            return None
