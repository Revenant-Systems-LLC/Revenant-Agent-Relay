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

_VIDEO_EXTS = {".mp4", ".mov"}
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
_SUPPORTED_EXTS = _VIDEO_EXTS | _IMAGE_EXTS

_ERROR_CAUSE = {
    "AUTH_ERROR": "Snapchat authentication failed or an account challenge blocked automation.",
    "MEDIA_ERROR": "Required media file was missing, unsupported, or rejected by Snapchat web flow.",
    "CONTENT_ERROR": "Snapchat web flow rejected caption/content fields.",
    "PLATFORM_AUTOMATION_ERROR": "Reliable Snapchat web publish/ad-draft confirmation was not available via current flow.",
    "NETWORK_ERROR": "Transient timeout/network failure while loading Snapchat pages.",
    "RATE_LIMIT_ERROR": "Snapchat temporarily throttled or action-blocked the account.",
}

_ERROR_FIX = {
    "AUTH_ERROR": "Verify RR_SNAPCHAT_USERNAME / RR_SNAPCHAT_PASSWORD and complete any required verification manually.",
    "MEDIA_ERROR": "Use an approved local .mp4/.mov file (or supported image only when the selected Snapchat flow allows it).",
    "CONTENT_ERROR": "Review content text and required fields in Snapchat Ads Manager.",
    "PLATFORM_AUTOMATION_ERROR": "Use a verified Snapchat Ads Manager business workflow/API setup, then re-validate selectors.",
    "NETWORK_ERROR": "Retry once after confirming network connectivity.",
    "RATE_LIMIT_ERROR": "Wait and retry later; reduce automation frequency.",
}

_TRANSIENT_TYPES = {"NETWORK_ERROR"}


def _resolve_media(media_path):
    p = Path(media_path)
    if p.is_absolute():
        return p
    return ROOT / p


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


class SnapchatAdapter:
    def __init__(self, snapchat_config, settings):
        if not PLAYWRIGHT_AVAILABLE:
            raise RuntimeError("playwright is not installed. Run: pip install playwright")

        cfg = snapchat_config or {}
        self.username_env = cfg.get("username_env_var", "RR_SNAPCHAT_USERNAME")
        self.password_env = cfg.get("password_env_var", "RR_SNAPCHAT_PASSWORD")
        self.post_target_env = cfg.get("post_target_env_var", "RR_SNAPCHAT_POST_TARGET")
        self.business_url_env = cfg.get("business_url_env_var", "RR_SNAPCHAT_BUSINESS_URL")

        self.username = os.getenv(self.username_env, "")
        self.password = os.getenv(self.password_env, "")
        self.post_target = os.getenv(self.post_target_env, "")
        self.business_url = os.getenv(self.business_url_env, "https://ads.snapchat.com")

        if not self.username or not self.password:
            raise RuntimeError(
                "Snapchat credentials missing. Set env vars: "
                f"{self.username_env}, {self.password_env}"
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
            if m_file.suffix.lower() in _SUPPORTED_EXTS:
                media_files.append(m_file)
                
        if not media_files:
            return _result(
                False,
                error_type="MEDIA_ERROR",
                detected="No supported media files provided for Snapchat.",
                raw="missing_media_path",
            )
            
        # Prioritize video files for Snapchat's video-first composer
        video_files = [f for f in media_files if f.suffix.lower() in _VIDEO_EXTS]
        resolved = video_files[0] if video_files else media_files[0]
        ext = resolved.suffix.lower()

        browser = None
        context = None
        page = None
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=self.headless)
                context = browser.new_context()
                page = context.new_page()

                blocker = self._open_and_auth(page)
                if blocker:
                    blocker["screenshot_path"] = self._capture_screenshot(page)
                    return _result(**blocker)

                if ext in _IMAGE_EXTS:
                    return _result(
                        False,
                        error_type="PLATFORM_AUTOMATION_ERROR",
                        detected="Snapchat web ad flow in this adapter is implemented as video-first; static image upload path is not reliably automatable here.",
                        raw=f"image_media_not_attempted:{ext}",
                        screenshot_path=self._capture_screenshot(page),
                    )

                outcome = self._attempt_ads_draft_flow(page, str(resolved), caption)
                if outcome.get("success"):
                    return _result(**outcome)

                outcome["screenshot_path"] = self._capture_screenshot(page)
                return _result(**outcome)
        except PlaywrightTimeoutError as e:
            return _result(
                False,
                error_type="NETWORK_ERROR",
                detected="Timeout while navigating Snapchat web pages.",
                raw=repr(e),
                screenshot_path=self._capture_screenshot(page),
            )
        except Exception as e:
            kind = self._classify_error(str(e))
            return _result(
                False,
                error_type=kind,
                detected=str(e),
                raw=repr(e),
                screenshot_path=self._capture_screenshot(page),
            )
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

    def _open_and_auth(self, page):
        page.goto(self.business_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(1500)

        if self._is_logged_in(page):
            return None

        self._perform_login(page)
        page.wait_for_timeout(2500)

        blocker = self._detect_auth_blocker(page)
        if blocker:
            return blocker

        if not self._is_logged_in(page):
            return {
                "success": False,
                "error_type": "AUTH_ERROR",
                "detected": "Unable to establish authenticated Snapchat session in Ads Manager.",
                "raw": f"not_authenticated:{page.url}",
                "post_url": None,
            }

        return None

    def _perform_login(self, page):
        login_markers = ["log in", "sign in", "username", "password"]
        content = (page.content() or "").lower()
        if not any(m in content for m in login_markers):
            try:
                page.goto("https://accounts.snapchat.com/accounts/login", wait_until="domcontentloaded", timeout=60000)
            except Exception:
                logger.debug("Failed to navigate to Snapchat login page", exc_info=True)

        user_inputs = [
            'input[name="username"]',
            'input[id*="username" i]',
            'input[type="email"]',
            'input[autocomplete="username"]',
            'input[placeholder*="username" i]',
            'input[placeholder*="email" i]',
        ]
        pass_inputs = [
            'input[name="password"]',
            'input[type="password"]',
            'input[autocomplete="current-password"]',
        ]

        user_filled = False
        for sel in user_inputs:
            loc = page.locator(sel).first
            if loc.count() > 0:
                loc.fill(self.username, timeout=4000)
                user_filled = True
                break

        pass_filled = False
        for sel in pass_inputs:
            loc = page.locator(sel).first
            if loc.count() > 0:
                loc.fill(self.password, timeout=4000)
                pass_filled = True
                break

        if user_filled and pass_filled:
            submit_selectors = [
                'button:has-text("Log In")',
                'button:has-text("Sign In")',
                'button[type="submit"]',
                'input[type="submit"]',
            ]
            clicked = False
            for sel in submit_selectors:
                btn = page.locator(sel).first
                if btn.count() > 0:
                    btn.click(timeout=5000)
                    clicked = True
                    break
            if not clicked:
                page.keyboard.press("Enter")

    def _is_logged_in(self, page):
        url = page.url.lower()
        if "ads.snapchat.com" in url and "login" not in url and "accounts.snapchat.com" not in url:
            return True

        indicators = [
            page.locator('text=Ads Manager').first,
            page.locator('text=Create Campaign').first,
            page.locator('[data-testid*="account" i]').first,
        ]
        for loc in indicators:
            try:
                if loc.count() > 0:
                    return True
            except Exception:
                continue
        return False

    def _detect_auth_blocker(self, page):
        content = (page.content() or "").lower()
        url = page.url.lower()

        auth_map = {
            "invalid credentials": "Invalid Snapchat username/password.",
            "incorrect password": "Invalid Snapchat username/password.",
            "wrong password": "Invalid Snapchat username/password.",
            "two-factor": "Snapchat 2FA challenge detected.",
            "verification code": "Snapchat verification challenge detected.",
            "security check": "Snapchat security challenge detected.",
            "captcha": "Snapchat captcha challenge detected.",
            "checkpoint": "Snapchat checkpoint detected.",
            "temporarily locked": "Snapchat account appears locked.",
            "locked": "Snapchat account appears locked.",
        }
        for needle, detected in auth_map.items():
            if needle in content:
                return {
                    "success": False,
                    "error_type": "AUTH_ERROR",
                    "detected": detected,
                    "raw": f"auth_blocker:{needle}",
                    "post_url": None,
                }

        if "rate" in content and "limit" in content:
            return {
                "success": False,
                "error_type": "RATE_LIMIT_ERROR",
                "detected": "Snapchat indicates temporary login/action throttling.",
                "raw": "auth_rate_limit_detected",
                "post_url": None,
            }

        if "accounts.snapchat.com" in url and ("login" in url or "verify" in url):
            return {
                "success": False,
                "error_type": "AUTH_ERROR",
                "detected": "Snapchat login still requires manual verification/challenge completion.",
                "raw": f"auth_flow_incomplete:{url}",
                "post_url": None,
            }
        return None

    def _attempt_ads_draft_flow(self, page, media_file, caption):
        page.goto(self.business_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)

        create_buttons = [
            page.get_by_role("button", name="Create Campaign"),
            page.get_by_role("button", name="Create"),
            page.locator('a:has-text("Create Campaign")'),
        ]
        create_clicked = False
        for btn in create_buttons:
            if btn.count() > 0:
                btn.first.click(timeout=5000)
                create_clicked = True
                break

        if not create_clicked:
            return {
                "success": False,
                "error_type": "PLATFORM_AUTOMATION_ERROR",
                "detected": "Snapchat Ads Manager campaign composer entry point not found.",
                "raw": "missing_create_campaign_entry",
                "post_url": None,
            }

        page.wait_for_timeout(2500)

        file_inputs = page.locator('input[type="file"]')
        if file_inputs.count() == 0:
            return {
                "success": False,
                "error_type": "PLATFORM_AUTOMATION_ERROR",
                "detected": "Snapchat ad composer opened, but no upload input was found.",
                "raw": "missing_upload_input",
                "post_url": None,
            }

        file_inputs.first.set_input_files(media_file)
        page.wait_for_timeout(3000)

        if caption:
            caption_fields = [
                page.locator('textarea[name*="headline" i]').first,
                page.locator('textarea[name*="caption" i]').first,
                page.locator('textarea').first,
                page.locator('input[name*="headline" i]').first,
                page.locator('input[name*="caption" i]').first,
            ]
            for field in caption_fields:
                if field.count() > 0:
                    field.fill(caption, timeout=5000)
                    break

        if self.post_target:
            target_fields = [
                page.locator('input[name*="url" i]').first,
                page.locator('input[placeholder*="url" i]').first,
                page.locator('input[placeholder*="website" i]').first,
            ]
            for field in target_fields:
                if field.count() > 0:
                    field.fill(self.post_target, timeout=5000)
                    break

        content = (page.content() or "").lower()
        if "unsupported" in content or "upload failed" in content or "couldn't upload" in content:
            return {
                "success": False,
                "error_type": "MEDIA_ERROR",
                "detected": "Snapchat rejected uploaded media in web ad flow.",
                "raw": "upload_rejected",
                "post_url": None,
            }

        publish_like = [
            page.get_by_role("button", name="Publish"),
            page.get_by_role("button", name="Submit"),
            page.get_by_role("button", name="Save"),
            page.get_by_role("button", name="Review and Publish"),
        ]
        url_before_submit = page.url
        clicked_submit = False
        for btn in publish_like:
            if btn.count() > 0:
                btn.first.click(timeout=5000)
                clicked_submit = True
                break

        if not clicked_submit:
            return {
                "success": False,
                "error_type": "PLATFORM_AUTOMATION_ERROR",
                "detected": "Snapchat ad composer loaded, but no clear publish/save control was detected.",
                "raw": "missing_publish_save_control",
                "post_url": None,
            }

        page.wait_for_timeout(4000)

        confirmation = self._detect_ads_confirmation(page, url_before_submit)
        if confirmation:
            return {
                "success": True,
                "error_type": None,
                "detected": confirmation["detected"],
                "raw": confirmation["raw"],
                "post_url": confirmation.get("post_url"),
            }

        return {
            "success": False,
            "error_type": "PLATFORM_AUTOMATION_ERROR",
            "detected": "Snapchat publish/save confirmation was ambiguous.",
            "raw": "No reliable Snapchat confirmation signal found after submit action (missing entity ID URL/link, specific success toast/dialog, confirmation page transition, or created-row match).",
            "post_url": None,
        }

    @staticmethod
    def _url_has_entity_id(url):
        if not url:
            return False
        normalized = url.strip().rstrip("/")
        if not normalized:
            return False
        tail = normalized.split("/")[-1].split("?")[0].split("#")[0]
        if not tail:
            return False
        # Accept IDs/tokens like numeric IDs, UUIDs, or slug-ish opaque handles.
        if len(tail) < 6:
            return False
        return any(ch.isdigit() for ch in tail) or "-" in tail or "_" in tail

    def _detect_ads_confirmation(self, page, url_before_submit):
        current_url = page.url or ""
        current_url_lower = current_url.lower()
        # A URL the page was already on before the click says nothing about
        # the click, so URL signals count only after a navigation.
        navigated = current_url != (url_before_submit or "")

        # 1) Created entity URL/ID capture (must include concrete ID/token).
        entity_url_markers = ["/campaigns/", "/ads/", "/ad-squad/"]
        if (
            navigated
            and any(marker in current_url_lower for marker in entity_url_markers)
            and self._url_has_entity_id(current_url)
        ):
            return {
                "detected": "Snapchat navigated to a created campaign/ad entity URL after submit.",
                "raw": "entity_url_with_id_detected",
                "post_url": current_url,
            }

        entity_link = page.locator('a[href*="/campaigns/"], a[href*="/ads/"], a[href*="/ad-squad/"]')
        if entity_link.count() > 0:
            href = entity_link.first.get_attribute("href")
            if href:
                normalized = href if href.startswith("http") else f"https://ads.snapchat.com{href}"
                if self._url_has_entity_id(normalized):
                    return {
                        "detected": "Snapchat exposed a created campaign/ad entity link after submit.",
                        "raw": "entity_link_with_id_detected",
                        "post_url": normalized,
                    }

        # 2) Snapchat-specific confirmation toasts/dialogs. A "Review your
        # campaign" dialog asks for a review before publishing; it confirms
        # nothing, so it is not one of them.
        success_toast = page.locator(
            '[role="alert"]:has-text("Campaign created"), '
            '[role="alert"]:has-text("Draft created"), '
            '[role="dialog"]:has-text("Campaign created")'
        )
        if success_toast.count() > 0:
            return {
                "detected": "Snapchat success toast/dialog appeared after submit.",
                "raw": "success_toast_or_dialog_detected",
                "post_url": current_url,
            }

        # 3) Confirmation/success route transition after submit.
        # Intentionally excludes generic '/draft' by itself, and '/review':
        # the review step comes before publishing, and "Review and Publish"
        # lands there whether or not anything was published.
        if navigated and any(seg in current_url_lower for seg in ["/confirm", "/confirmation", "/success"]):
            return {
                "detected": "Snapchat navigated to a confirmation/success route after submit.",
                "raw": "confirmation_route_detected",
                "post_url": current_url,
            }

        return None

    def _capture_screenshot(self, page):
        if page is None:
            return None
        out_dir = LOGS_DIR / datetime.now().date().isoformat() / "failures"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"snapchat-{datetime.now().strftime('%Y%m%d-%H%M%S')}.png"
        try:
            page.screenshot(path=str(path), full_page=True)
            return path
        except Exception:
            return None

    def _classify_error(self, msg):
        low = (msg or "").lower()
        if "429" in low or "rate" in low or "too many" in low or "throttl" in low:
            return "RATE_LIMIT_ERROR"
        if "timeout" in low or "net::" in low or "connection" in low:
            return "NETWORK_ERROR"
        if any(x in low for x in ["password", "login", "captcha", "verify", "checkpoint", "locked"]):
            return "AUTH_ERROR"
        if any(x in low for x in ["upload", "media", "file"]):
            return "MEDIA_ERROR"
        if any(x in low for x in ["caption", "headline", "content"]):
            return "CONTENT_ERROR"
        return "PLATFORM_AUTOMATION_ERROR"
