import logging
import os
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    PlaywrightTimeoutError = Exception

from ..paths import LOGS_DIR, ROOT

_VIDEO_EXTS = {".mp4", ".mov", ".webm", ".m4v"}
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
_SUPPORTED_EXTS = _VIDEO_EXTS | _IMAGE_EXTS

_ERROR_CAUSE = {
    "AUTH_ERROR": "TikTok credentials are missing, invalid, or blocked by challenge.",
    "MEDIA_ERROR": "Approved media file is missing, unsupported, or upload failed.",
    "PLATFORM_AUTOMATION_ERROR": "TikTok UI flow changed or reliable publish confirmation was not possible.",
    "NETWORK_ERROR": "Transient network or timeout failure while loading TikTok.",
    "RATE_LIMIT_ERROR": "TikTok temporarily limited posting actions.",
}

_ERROR_FIX = {
    "AUTH_ERROR": "Verify RR_TIKTOK_USERNAME / RR_TIKTOK_PASSWORD and complete any account verification manually.",
    "MEDIA_ERROR": "Provide a valid approved media path; prefer MP4/MOV video for TikTok.",
    "PLATFORM_AUTOMATION_ERROR": "Re-verify TikTok selectors and flow, then retry once manually validated.",
    "NETWORK_ERROR": "Retry once; likely transient connectivity or page timeout.",
    "RATE_LIMIT_ERROR": "Wait before retrying and reduce posting frequency.",
}

_TRANSIENT_TYPES = {"NETWORK_ERROR"}


def _resolve_media(media_path):
    p = Path(media_path)
    if p.is_absolute():
        return p
    return ROOT / p


def _is_supported_media(path_obj):
    return path_obj.suffix.lower() in _SUPPORTED_EXTS


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


class TikTokAdapter:
    def __init__(self, tiktok_config, settings):
        if not PLAYWRIGHT_AVAILABLE:
            raise RuntimeError("playwright is not installed. Run: pip install playwright")

        cfg = tiktok_config or {}
        self.username_env = cfg.get("username_env_var", "RR_TIKTOK_USERNAME")
        self.password_env = cfg.get("password_env_var", "RR_TIKTOK_PASSWORD")
        self.enable_automation_env = cfg.get("enable_automation_env_var", "RR_TIKTOK_ENABLE_AUTOMATION")

        self.username = os.getenv(self.username_env, "")
        self.password = os.getenv(self.password_env, "")
        self.enable_automation = os.getenv(self.enable_automation_env, "").lower() == "true"

        if not self.username or not self.password:
            raise RuntimeError(
                "TikTok credentials missing. Set env vars: "
                f"{self.username_env}, {self.password_env}"
            )

        # Raise here rather than failing every post: with automation off the
        # adapter can only fail, and an init error is what lets load_adapter()
        # treat TikTok as unavailable.
        if not self.enable_automation:
            raise RuntimeError(
                "TikTok automation is disabled. "
                f"Set {self.enable_automation_env}=true to run browser flow."
            )

        mode = (settings or {}).get("mode", "dev")
        mode_settings = (settings or {}).get(f"{mode}_mode", {})
        self.headless = mode_settings.get("headless", mode == "scheduled")

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
            if _is_supported_media(m_file) and m_file.suffix.lower() in _VIDEO_EXTS:
                media_files.append(m_file)
                
        if not media_files:
            return _result(
                False,
                error_type="MEDIA_ERROR",
                detected="TikTok adapter requires video media; no valid video assets were provided in media_paths.",
                raw="missing_video_media",
            )
            
        resolved = media_files[0]

        browser = None
        context = None
        page = None
        screenshot_path = None
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=self.headless)
                context = browser.new_context()
                page = context.new_page()

                login_result = self._login_if_needed(page)
                if login_result:
                    login_result["screenshot_path"] = self._capture_screenshot(page)
                    return _result(**login_result)

                upload_result = self._upload_media(page, str(resolved))
                if upload_result:
                    upload_result["screenshot_path"] = self._capture_screenshot(page)
                    return _result(**upload_result)

                caption_result = self._fill_caption(page, caption)
                if caption_result:
                    caption_result["screenshot_path"] = self._capture_screenshot(page)
                    return _result(**caption_result)

                success_url = self._detect_post_success(page)
                if success_url:
                    return _result(True, post_url=success_url)

                screenshot_path = self._capture_screenshot(page)
                return _result(
                    False,
                    error_type="PLATFORM_AUTOMATION_ERROR",
                    detected="Upload/caption preparation completed, but final publish was not auto-confirmed safely.",
                    raw="publish_not_confirmed",
                    screenshot_path=screenshot_path,
                )
        except PlaywrightTimeoutError as e:
            screenshot_path = self._capture_screenshot(page)
            return _result(
                False,
                error_type="NETWORK_ERROR",
                detected="Timeout while navigating TikTok pages.",
                raw=repr(e),
                screenshot_path=screenshot_path,
            )
        except Exception as e:
            screenshot_path = self._capture_screenshot(page)
            kind = self._classify_error(str(e))
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

    def _login_if_needed(self, page):
        page.goto("https://www.tiktok.com/upload", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(1500)
        current_url = page.url.lower()

        if "/login" in current_url or "login" in page.title().lower():
            username_candidates = [
                'input[name="username"]',
                'input[type="text"]',
                'input[placeholder*="Email" i]',
                'input[autocomplete="username"]',
            ]
            password_candidates = [
                'input[type="password"]',
                'input[name="password"]',
                'input[autocomplete="current-password"]',
            ]

            user_filled = False
            for sel in username_candidates:
                loc = page.locator(sel).first
                if loc.count() > 0:
                    loc.fill(self.username, timeout=5000)
                    user_filled = True
                    break

            pass_filled = False
            for sel in password_candidates:
                loc = page.locator(sel).first
                if loc.count() > 0:
                    loc.fill(self.password, timeout=5000)
                    pass_filled = True
                    break

            if user_filled and pass_filled:
                page.keyboard.press("Enter")
                page.wait_for_timeout(4000)

        content = page.content().lower()
        url_now = page.url.lower()

        challenge_markers = ["captcha", "verify", "two-step", "2-step", "security check", "qr code", "code sent"]
        if any(m in content for m in challenge_markers):
            return {
                "success": False,
                "error_type": "AUTH_ERROR",
                "detected": "TikTok login challenge detected (captcha/verification/2FA).",
                "raw": "auth_challenge_detected",
                "post_url": None,
            }

        if "/login" in url_now:
            return {
                "success": False,
                "error_type": "AUTH_ERROR",
                "detected": "Unable to establish authenticated TikTok session.",
                "raw": f"still_on_login:{page.url}",
                "post_url": None,
            }

        return None

    def _upload_media(self, page, media_file):
        page.goto("https://www.tiktok.com/upload", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)

        file_inputs = page.locator('input[type="file"]')
        if file_inputs.count() == 0:
            return {
                "success": False,
                "error_type": "PLATFORM_AUTOMATION_ERROR",
                "detected": "TikTok upload file input selector not found.",
                "raw": "missing_upload_input",
                "post_url": None,
            }

        file_inputs.first.set_input_files(media_file)
        page.wait_for_timeout(5000)

        content = page.content().lower()
        if "upload failed" in content or "couldn't upload" in content or "could not upload" in content:
            return {
                "success": False,
                "error_type": "MEDIA_ERROR",
                "detected": "TikTok reported media upload failure.",
                "raw": "upload_failed",
                "post_url": None,
            }

        return None

    def _fill_caption(self, page, caption):
        caption_selectors = [
            'div[contenteditable="true"][role="textbox"]',
            '[data-e2e="video-caption"]',
            'textarea[placeholder*="Describe" i]',
            'textarea[maxlength]',
        ]

        for sel in caption_selectors:
            loc = page.locator(sel).first
            if loc.count() > 0:
                loc.click(timeout=5000)
                loc.fill(caption, timeout=10000)
                return None

        return {
            "success": False,
            "error_type": "PLATFORM_AUTOMATION_ERROR",
            "detected": "Caption field selector not found in TikTok upload UI.",
            "raw": "missing_caption_field",
            "post_url": None,
        }

    def _detect_post_success(self, page):
        url = page.url
        if "/@" in url and "/video/" in url:
            return url

        html = page.content().lower()
        if "posted" in html and "/video/" in html:
            return url if url.startswith("https://www.tiktok.com/") else None
        return None

    def _classify_error(self, msg):
        m = (msg or "").lower()
        if "429" in m or "too many" in m or "rate" in m or "temporarily blocked" in m:
            return "RATE_LIMIT_ERROR"
        if "captcha" in m or "2fa" in m or "two-step" in m or "login" in m or "password" in m:
            return "AUTH_ERROR"
        if "upload" in m or "media" in m or "file" in m:
            return "MEDIA_ERROR"
        if "timeout" in m or "net::" in m or "connection" in m:
            return "NETWORK_ERROR"
        return "PLATFORM_AUTOMATION_ERROR"

    def _capture_screenshot(self, page):
        if page is None:
            return None
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out_dir = LOGS_DIR / datetime.now().date().isoformat() / "failures"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"tiktok-{stamp}.png"
        try:
            page.screenshot(path=str(path), full_page=True)
            return path
        except Exception:
            return None
