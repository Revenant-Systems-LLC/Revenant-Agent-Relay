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

_X_MAX_POST_CHARS = 280
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
_VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm"}
_SUPPORTED_MEDIA_EXTS = _IMAGE_EXTS | _VIDEO_EXTS

_ERROR_CAUSE = {
    "AUTH_ERROR": "X authentication failed or account access is blocked by challenge.",
    "MEDIA_ERROR": "Media file missing, unsupported, rejected, or upload failed on X.",
    "CONTENT_ERROR": "Caption/content is invalid or violates X posting constraints.",
    "RATE_LIMIT_ERROR": "X temporarily throttled posting or blocked action due to limits.",
    "PLATFORM_AUTOMATION_ERROR": "X compose/post UI could not be automated reliably.",
    "NETWORK_ERROR": "Transient timeout or network issue while loading X.",
}

_ERROR_FIX = {
    "AUTH_ERROR": "Verify RR_X_USERNAME / RR_X_PASSWORD and complete any account challenge manually.",
    "MEDIA_ERROR": "Use a valid local media file with supported format and retry.",
    "CONTENT_ERROR": "Ensure caption is non-empty and within X character constraints.",
    "RATE_LIMIT_ERROR": "Wait and retry later; avoid rapid repeated post attempts.",
    "PLATFORM_AUTOMATION_ERROR": "Re-validate X selectors/UI flow and retry after adapter update.",
    "NETWORK_ERROR": "Retry once after network stabilizes.",
}

_TRANSIENT_TYPES = {"NETWORK_ERROR"}

# Confirmation note for an action block or rate limit shown after submitting.
_RATE_LIMIT_NOTE = "x_rate_limit_or_action_block"


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


class XAdapter:
    def __init__(self, x_config, settings):
        if not PLAYWRIGHT_AVAILABLE:
            raise RuntimeError("playwright is not installed. Run: pip install playwright")

        cfg = x_config or {}
        self.username_env = cfg.get("username_env_var", "RR_X_USERNAME")
        self.password_env = cfg.get("password_env_var", "RR_X_PASSWORD")
        self.username = os.getenv(self.username_env, "")
        self.password = os.getenv(self.password_env, "")

        if not self.username or not self.password:
            raise RuntimeError(
                "X credentials missing. Set env vars: "
                f"{self.username_env}, {self.password_env}"
            )

        self.settings = settings or {}
        mode = (settings or {}).get("mode", "dev")
        mode_settings = (settings or {}).get(f"{mode}_mode", {})
        self.headless = mode_settings.get("headless", mode == "scheduled")

    def post_ad(self, ad, caption):
        if not isinstance(caption, str) or not caption.strip():
            return _result(
                False,
                error_type="CONTENT_ERROR",
                detected="Caption is empty or invalid.",
                raw="invalid_caption",
            )

        if len(caption) > _X_MAX_POST_CHARS:
            return _result(
                False,
                error_type="CONTENT_ERROR",
                detected=f"Caption exceeds X limit of {_X_MAX_POST_CHARS} characters.",
                raw=f"caption_len={len(caption)}",
            )

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
            if m_file.suffix.lower() not in _SUPPORTED_MEDIA_EXTS:
                return _result(
                    False,
                    error_type="MEDIA_ERROR",
                    detected=f"Unsupported media extension for X: {m_file.suffix}",
                    raw=str(m_file),
                )
            media_files.append(str(m_file))

        browser = None
        context = None
        page = None
        screenshot_path = None
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=self.headless)
                context = browser.new_context()
                page = context.new_page()

                try:
                    login_error = self._ensure_logged_in(page)
                    if login_error:
                        login_error["screenshot_path"] = self._capture_screenshot(page)
                        return _result(**login_error)

                    compose_error = self._open_compose(page)
                    if compose_error:
                        compose_error["screenshot_path"] = self._capture_screenshot(page)
                        return _result(**compose_error)

                    fill_error = self._fill_caption(page, caption)
                    if fill_error:
                        fill_error["screenshot_path"] = self._capture_screenshot(page)
                        return _result(**fill_error)

                    if media_files:
                        media_error = self._attach_media(page, media_files)
                        if media_error:
                            media_error["screenshot_path"] = self._capture_screenshot(page)
                            return _result(**media_error)

                    url_before_submit = page.url
                    submit_error = self._submit_post(page)
                    if submit_error:
                        submit_error["screenshot_path"] = self._capture_screenshot(page)
                        return _result(**submit_error)

                    success_url, confirmation_note, uncertain = self._detect_post_confirmation(page, url_before_submit)
                    if uncertain:
                        screenshot = self._capture_screenshot(page)
                        if confirmation_note == _RATE_LIMIT_NOTE:
                            return _result(
                                False,
                                error_type="RATE_LIMIT_ERROR",
                                detected="X blocked the post or reported a rate limit after submit.",
                                raw=confirmation_note,
                                screenshot_path=screenshot,
                            )
                        return _result(
                            False,
                            error_type="PLATFORM_AUTOMATION_ERROR",
                            detected="Unable to reliably confirm X post success.",
                            raw=confirmation_note,
                            screenshot_path=screenshot,
                        )

                    # Passive Lead Scout Sweep
                    try:
                        if self.settings.get("lead_scout", {}).get("enabled", True):
                            from ..scout_logger import log_leads
                            leads = self.scout_leads(page, self.settings)
                            log_leads(leads)
                    except Exception as e:
                        print(f"[X Scout] Error running Lead Scout: {e}")

                    if success_url:
                        return _result(True, post_url=success_url)

                    return _result(
                        True,
                        post_url=None,
                        detected="X UI indicated post completion, but no canonical post URL was captured.",
                        raw=confirmation_note,
                    )
                except Exception:
                    # Capture now, while the browser is still alive. The handlers
                    # below run after sync_playwright() has torn everything
                    # down, where page.screenshot() can only fail.
                    screenshot_path = self._capture_screenshot(page)
                    raise
        except PlaywrightTimeoutError as exc:
            return _result(
                False,
                error_type="NETWORK_ERROR",
                detected="Timeout while interacting with X pages.",
                raw=repr(exc),
                screenshot_path=screenshot_path,
            )
        except Exception as exc:
            error_type = self._classify_exception(exc)
            return _result(
                False,
                error_type=error_type,
                detected=str(exc),
                raw=repr(exc),
                screenshot_path=screenshot_path,
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

    def scout_leads(self, page, settings):
        """
        Scan X for keywords defined in settings using the active browser page.
        Returns a list of discovered lead dictionaries.
        """
        scout_cfg = settings.get("lead_scout", {})
        if not scout_cfg.get("enabled", True):
            return []

        keywords = scout_cfg.get("keywords", [])
        limit = min(scout_cfg.get("max_leads_per_run", 5), 3) # Cap X sweeps lower to prevent rate limit
        leads = []

        import urllib.parse
        print(f"[X Scout] Starting passive sweep for {len(keywords)} keywords on active page...")
        try:
            for kw in keywords:
                query_encoded = urllib.parse.quote(kw)
                search_url = f"https://x.com/search?q={query_encoded}&f=live"
                page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(2000)

                # Wait for tweets to render
                try:
                    page.wait_for_selector('article[data-testid="tweet"]', timeout=8000)
                except Exception:
                    # Skip if no tweets found or timed out
                    continue

                tweet_articles = page.locator('article[data-testid="tweet"]').all()
                count = 0
                for article in tweet_articles:
                    if count >= limit:
                        break
                    
                    try:
                        # Extract author handle
                        author_loc = article.locator('[data-testid="User-Name"]').first
                        author_text = author_loc.inner_text() if author_loc.count() > 0 else ""
                        if not author_text:
                            continue
                        # format e.g. "Name\n@handle\n·\n1h"
                        lines = author_text.split("\n")
                        handle = lines[1] if len(lines) > 1 and lines[1].startswith("@") else lines[0]

                        # Avoid capturing ourselves
                        if self.username and self.username.lower() in handle.lower():
                            continue

                        # Extract tweet content
                        text_loc = article.locator('[data-testid="tweetText"]').first
                        content = text_loc.inner_text() if text_loc.count() > 0 else ""
                        if not content:
                            continue

                        # Extract tweet status URL or link
                        link_loc = article.locator('a[href*="/status/"]').first
                        status_path = link_loc.get_attribute("href") if link_loc.count() > 0 else ""
                        tweet_url = f"https://x.com{status_path}" if status_path else ""
                        if not tweet_url:
                            continue

                        leads.append({
                            "platform": "x",
                            "keyword": kw,
                            "author": handle,
                            "post_content": content[:500],
                            "post_url": tweet_url
                        })
                        count += 1
                    except Exception:
                        continue
        except Exception as e:
            print(f"[X Scout] Warning: Passive sweep encountered an issue: {e}")

        return leads

    def _ensure_logged_in(self, page):
        page.goto("https://x.com/compose/post", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(1500)

        if self._is_logged_in(page):
            return None

        page.goto("https://x.com/i/flow/login", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(1200)

        username_input = page.locator('input[autocomplete="username"], input[name="text"]').first
        if username_input.count() == 0:
            return self._auth_error("X login username field not found.", "missing_username_input")
        username_input.fill(self.username)
        page.keyboard.press("Enter")
        page.wait_for_timeout(1800)

        if self._contains_text(page, ["enter your phone number", "confirm your identity", "challenge"]):
            return self._auth_error("X security challenge/checkpoint detected after username entry.", "checkpoint_challenge")

        password_input = page.locator('input[type="password"]').first
        if password_input.count() == 0:
            if self._contains_text(page, ["verification code", "two-factor", "2fa", "authentication app"]):
                return self._auth_error("X 2FA challenge detected; automated login cannot proceed.", "two_factor_required")
            return self._auth_error("X password field not available.", "missing_password_input")

        password_input.fill(self.password)
        page.keyboard.press("Enter")
        page.wait_for_timeout(3500)

        blocker = self._detect_auth_blocker(page)
        if blocker:
            return blocker

        if not self._is_logged_in(page):
            return self._auth_error("Unable to confirm authenticated X session after login attempt.", f"still_unauthed:{page.url}")

        return None

    def _open_compose(self, page):
        page.goto("https://x.com/compose/post", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(1200)
        composer = page.locator('div[role="textbox"][data-testid="tweetTextarea_0"]').first
        if composer.count() == 0:
            composer = page.locator('div[role="textbox"][aria-label*="Post" i], div[data-testid="tweetTextarea_0"]').first
        if composer.count() == 0:
            return {
                "success": False,
                "error_type": "PLATFORM_AUTOMATION_ERROR",
                "detected": "X compose text area not found.",
                "raw": f"compose_missing:{page.url}",
                "post_url": None,
            }
        return None

    def _fill_caption(self, page, caption):
        composer = page.locator('div[role="textbox"][data-testid="tweetTextarea_0"], div[data-testid="tweetTextarea_0"]').first
        composer.click(timeout=5000)
        composer.fill(caption)
        page.wait_for_timeout(500)

        too_long_markers = [
            "0 characters remaining",
            "your post is too long",
            "reduce by",
            "post cannot exceed",
        ]
        if self._contains_text(page, too_long_markers):
            return {
                "success": False,
                "error_type": "CONTENT_ERROR",
                "detected": "X composer reports caption length/content error.",
                "raw": "composer_rejected_caption",
                "post_url": None,
            }
        return None

    def _attach_media(self, page, media_files):
        file_input = page.locator('input[data-testid="fileInput"], input[type="file"]').first
        if file_input.count() == 0:
            return {
                "success": False,
                "error_type": "MEDIA_ERROR",
                "detected": "X media file input not found in composer.",
                "raw": "missing_media_input",
                "post_url": None,
            }

        file_input.set_input_files(media_files)
        page.wait_for_timeout(5000)

        upload_error_markers = [
            "some of your media failed to upload",
            "media processing failed",
            "this file format is not supported",
            "image dimensions",
            "media could not be uploaded",
        ]
        if self._contains_text(page, upload_error_markers):
            return {
                "success": False,
                "error_type": "MEDIA_ERROR",
                "detected": "X reported media upload failure or rejection.",
                "raw": "media_upload_rejected",
                "post_url": None,
            }
        return None

    def _submit_post(self, page):
        post_btn = page.locator('button[data-testid="tweetButtonInline"], button[data-testid="tweetButton"]').first
        if post_btn.count() == 0 or post_btn.is_disabled():
            return {
                "success": False,
                "error_type": "PLATFORM_AUTOMATION_ERROR",
                "detected": "X Post button unavailable or disabled.",
                "raw": "post_button_unavailable",
                "post_url": None,
            }
        post_btn.click(timeout=7000)
        page.wait_for_timeout(3500)

        if self._contains_text(page, ["try again", "post was not sent", "something went wrong"]):
            return {
                "success": False,
                "error_type": "PLATFORM_AUTOMATION_ERROR",
                "detected": "X indicated post submission failed.",
                "raw": "post_submit_failed",
                "post_url": None,
            }
        return None

    def _detect_post_confirmation(self, page, url_before_submit):
        # Only evidence tied to this submit counts. The composer closes onto
        # the home timeline, so a /status/ link elsewhere on the page is
        # usually someone else's post and says nothing about ours.
        if page.url != url_before_submit and self._is_own_status_url(page.url):
            return page.url, "own_status_url_after_submit", False

        send_toast = page.locator('[role="status"]:has-text("Your post was sent"), [data-testid="toast"]:has-text("Your post was sent")')
        if send_toast.count() > 0:
            # The toast's "View" link is the post that was just sent.
            toast_link = send_toast.first.locator('a[href*="/status/"]')
            if toast_link.count() > 0:
                href = toast_link.first.get_attribute("href")
                if href:
                    if href.startswith("http"):
                        return href, "send_toast_link_detected", False
                    return f"https://x.com{href}", "send_toast_link_detected", False
            return None, "explicit_send_toast_detected", False

        html = page.content().lower()
        if "action blocked" in html or "rate limit" in html or "try again later" in html:
            return None, _RATE_LIMIT_NOTE, True

        return None, "no_reliable_confirmation", True

    def _detect_auth_blocker(self, page):
        html = page.content().lower()
        if "wrong password" in html or "incorrect" in html or "could not verify" in html:
            return self._auth_error("X rejected credentials.", "invalid_credentials")
        if "two-factor" in html or "2fa" in html or "verification code" in html:
            return self._auth_error("X 2FA challenge detected.", "two_factor_required")
        if "captcha" in html or "arkose" in html:
            return self._auth_error("X captcha challenge detected.", "captcha_challenge")
        if "security challenge" in html or "confirm your identity" in html or "suspicious login" in html:
            return self._auth_error("X security/suspicious-login challenge detected.", "security_challenge")
        if "account locked" in html or "temporarily restricted" in html:
            return self._auth_error("X account locked or temporarily restricted.", "account_locked")
        return None

    def _is_own_status_url(self, url):
        """True for a status URL under this account's @handle.

        The handle is RR_X_USERNAME, so when that is an email or phone number
        nothing matches here and confirmation rests on the send toast.
        """
        parsed = urlparse(url or "")
        host = (parsed.hostname or "").lower()
        if host not in ("x.com", "twitter.com") and not host.endswith((".x.com", ".twitter.com")):
            return False
        parts = [part for part in parsed.path.split("/") if part]
        handle = self.username.strip().lstrip("@").lower()
        return len(parts) >= 3 and parts[0].lower() == handle and parts[1] == "status" and parts[2].isdigit()

    def _is_logged_in(self, page):
        if "x.com/home" in page.url.lower() or "x.com/compose" in page.url.lower():
            return True
        return page.locator('[data-testid="SideNav_NewTweet_Button"], [data-testid="AppTabBar_Home_Link"]').count() > 0

    @staticmethod
    def _contains_text(page, markers):
        content = page.content().lower()
        return any(m.lower() in content for m in markers)

    @staticmethod
    def _auth_error(detected, raw):
        return {
            "success": False,
            "error_type": "AUTH_ERROR",
            "detected": detected,
            "raw": raw,
            "post_url": None,
        }

    def _classify_exception(self, exc):
        msg = str(exc).lower()
        if "429" in msg or "rate limit" in msg or "action blocked" in msg:
            return "RATE_LIMIT_ERROR"
        if "net::" in msg or "timed out" in msg or "connection" in msg:
            return "NETWORK_ERROR"
        if "captcha" in msg or "login" in msg or "password" in msg or "auth" in msg:
            return "AUTH_ERROR"
        if "upload" in msg or "media" in msg or "file" in msg:
            return "MEDIA_ERROR"
        if "caption" in msg or "content" in msg or "tweet" in msg or "post" in msg:
            return "CONTENT_ERROR"
        return "PLATFORM_AUTOMATION_ERROR"

    def _capture_screenshot(self, page):
        if page is None:
            return None
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out_dir = LOGS_DIR / datetime.now().date().isoformat() / "failures"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"x-{stamp}.png"
        try:
            page.screenshot(path=str(path), full_page=True)
            return path
        except Exception:
            return None
