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
    "AUTH_ERROR": "LinkedIn authentication failed or account access is blocked by a challenge.",
    "MEDIA_ERROR": "Media file was missing, unsupported, or rejected by LinkedIn.",
    "PLATFORM_AUTOMATION_ERROR": "LinkedIn compose/publish UI flow was not reliably automatable.",
    "NETWORK_ERROR": "Transient timeout or connectivity issue while using LinkedIn.",
    "RATE_LIMIT_ERROR": "LinkedIn temporarily blocked or throttled posting actions.",
    "CONTENT_ERROR": "LinkedIn rejected the post content.",
}

_ERROR_FIX = {
    "AUTH_ERROR": "Verify LinkedIn credentials and ensure the account can pass any manual verification steps.",
    "MEDIA_ERROR": "Confirm ad.media_path exists and uses a supported image/video format.",
    "PLATFORM_AUTOMATION_ERROR": "Re-validate selectors and posting flow in the LinkedIn adapter.",
    "NETWORK_ERROR": "Retry the run; if persistent, validate local network and LinkedIn availability.",
    "RATE_LIMIT_ERROR": "Wait before retrying and reduce posting frequency.",
    "CONTENT_ERROR": "Adjust approved caption/media to comply with LinkedIn posting rules.",
}

_TRANSIENT_TYPES = {"NETWORK_ERROR"}



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


def _resolve_media(media_path):
    p = Path(media_path)
    if p.is_absolute():
        return p
    return ROOT / p


def _linkedin_path(url):
    """Lowercased path of a linkedin.com URL, or None for any other host.

    URL checks look at the path only, never the whole string: a query string
    can name another LinkedIn page.
    """
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
        return None
    return parsed.path.lower()


class LinkedInAdapter:
    def __init__(self, linkedin_config, settings):
        if not PLAYWRIGHT_AVAILABLE:
            raise RuntimeError("playwright is not installed. Run: pip install playwright")

        cfg = linkedin_config or {}
        self.username_env = cfg.get("username_env_var", "RR_LINKEDIN_USERNAME")
        self.password_env = cfg.get("password_env_var", "RR_LINKEDIN_PASSWORD")
        self.post_target_env = cfg.get("post_target_env_var", "RR_LINKEDIN_POST_TARGET")
        self.company_page_url_env = cfg.get("company_page_url_env_var", "RR_LINKEDIN_COMPANY_PAGE_URL")

        self.username = os.getenv(self.username_env, "").strip()
        self.password = os.getenv(self.password_env, "").strip()
        self.post_target = os.getenv(self.post_target_env, "company").strip().lower()
        self.company_page_url = os.getenv(self.company_page_url_env, "").strip()

        if not self.username or not self.password:
            raise RuntimeError(
                "LinkedIn credentials missing. Set env vars: "
                f"{self.username_env}, {self.password_env}"
            )

        if self.post_target not in {"company", "profile"}:
            raise RuntimeError(
                f"Unsupported LinkedIn post target '{self.post_target}'. "
                "Use 'company' or 'profile'."
            )

        if self.post_target == "company" and not self.company_page_url:
            raise RuntimeError(
                "LinkedIn company target requires company page URL env var: "
                f"{self.company_page_url_env}"
            )

        self.settings = settings or {}
        mode = (settings or {}).get("mode", "dev")
        mode_settings = (settings or {}).get(f"{mode}_mode", {})
        self.headless = mode_settings.get("headless", mode == "scheduled")

    def post_ad(self, ad, caption):
        if caption is None:
            caption = ""

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

        browser = None
        context = None
        page = None
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=self.headless)
                context = browser.new_context()
                page = context.new_page()

                login_gate = self._ensure_authenticated(page)
                if login_gate:
                    login_gate["screenshot_path"] = self._capture_screenshot(page)
                    return _result(**login_gate)

                nav_gate = self._navigate_to_post_surface(page)
                if nav_gate:
                    nav_gate["screenshot_path"] = self._capture_screenshot(page)
                    return _result(**nav_gate)

                compose_gate = self._open_composer(page)
                if compose_gate:
                    compose_gate["screenshot_path"] = self._capture_screenshot(page)
                    return _result(**compose_gate)

                caption_gate = self._fill_caption(page, caption)
                if caption_gate:
                    caption_gate["screenshot_path"] = self._capture_screenshot(page)
                    return _result(**caption_gate)

                media_gate = self._attach_media_if_any(page, media_files)
                if media_gate:
                    media_gate["screenshot_path"] = self._capture_screenshot(page)
                    return _result(**media_gate)

                publish_gate = self._publish_if_clear(page)
                if publish_gate:
                    publish_gate["screenshot_path"] = self._capture_screenshot(page)
                    return _result(**publish_gate)

                return self._confirm_publish(page)

        except PlaywrightTimeoutError as e:
            return _result(
                False,
                error_type="NETWORK_ERROR",
                detected="Timeout while navigating LinkedIn pages.",
                raw=repr(e),
                screenshot_path=self._capture_screenshot(page),
            )
        except Exception as e:
            error_type = self._classify_error(str(e))
            return _result(
                False,
                error_type=error_type,
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

    def scout_leads(self, page, settings):
        """
        Scan LinkedIn for keywords defined in settings using the active browser page.
        Returns a list of discovered lead dictionaries.
        """
        scout_cfg = settings.get("lead_scout", {})
        if not scout_cfg.get("enabled", True):
            return []

        keywords = scout_cfg.get("keywords", [])
        limit = min(scout_cfg.get("max_leads_per_run", 5), 3) # Cap LinkedIn sweeps lower to prevent rate limit
        leads = []

        import urllib.parse
        print(f"[LinkedIn Scout] Starting passive sweep for {len(keywords)} keywords on active page...")
        try:
            for kw in keywords:
                query_encoded = urllib.parse.quote(kw)
                search_url = f"https://www.linkedin.com/search/results/content/?keywords={query_encoded}&origin=GLOBAL_SEARCH_HEADER"
                page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(2000)

                # Wait for search results container
                try:
                    page.wait_for_selector('.reusable-search__result-container', timeout=8000)
                except Exception:
                    continue

                containers = page.locator('.reusable-search__result-container').all()
                count = 0
                for container in containers:
                    if count >= limit:
                        break
                    
                    try:
                        # Extract author name
                        author_loc = container.locator('.entity-result__title-text, .app-shared-outline-decorator').first
                        author = author_loc.inner_text().strip() if author_loc.count() > 0 else ""
                        if not author:
                            continue
                        author = author.split("\n")[0]  # strip subtitles or degrees

                        # Extract post content text
                        content_loc = container.locator('.feed-shared-update-v2__description-text, .entity-result__summary').first
                        content = content_loc.inner_text().strip() if content_loc.count() > 0 else ""
                        if not content:
                            continue

                        # Extract post URL or link
                        link_loc = container.locator('a[href*="/feed/update/urn:li:activity:"]').first
                        post_path = link_loc.get_attribute("href") if link_loc.count() > 0 else ""
                        post_url = post_path
                        if post_url and post_url.startswith("/"):
                            post_url = f"https://www.linkedin.com{post_url}"
                        
                        if not post_url:
                            continue

                        leads.append({
                            "platform": "linkedin",
                            "keyword": kw,
                            "author": author,
                            "post_content": content[:500],
                            "post_url": post_url
                        })
                        count += 1
                    except Exception:
                        continue
        except Exception as e:
            print(f"[LinkedIn Scout] Warning: Passive sweep encountered an issue: {e}")

        return leads

    def _ensure_authenticated(self, page):
        page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(1500)

        if self._is_logged_in(page):
            return None

        page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded", timeout=60000)
        page.locator('input[name="session_key"]').first.fill(self.username)
        page.locator('input[name="session_password"]').first.fill(self.password)
        page.get_by_role("button", name="Sign in").first.click()
        page.wait_for_timeout(3500)

        blocker = self._detect_auth_blocker(page)
        if blocker:
            return blocker

        if not self._is_logged_in(page):
            return {
                "success": False,
                "error_type": "AUTH_ERROR",
                "detected": "Unable to establish logged-in LinkedIn session.",
                "raw": f"still_unauthenticated:{page.url}",
                "post_url": None,
            }

        return None

    def _is_logged_in(self, page):
        # The path, not the whole URL: auth-wall and checkpoint redirects can
        # carry /feed/ in their query string.
        path = _linkedin_path(page.url)
        if path is not None and (path == "/feed" or path.startswith("/feed/")):
            return True
        if page.locator('button[aria-label*="Start a post"]').count() > 0:
            return True
        if page.locator('a[href*="/mynetwork/"]').count() > 0:
            return True
        return False

    def _detect_auth_blocker(self, page):
        body = page.content().lower()
        url = page.url.lower()
        if "invalid" in body and "password" in body:
            return {"success": False, "error_type": "AUTH_ERROR", "detected": "Invalid LinkedIn credentials.", "raw": "invalid_credentials", "post_url": None}
        if "checkpoint" in url or "checkpoint" in body:
            return {"success": False, "error_type": "AUTH_ERROR", "detected": "LinkedIn checkpoint challenge detected.", "raw": "checkpoint", "post_url": None}
        if "captcha" in body:
            return {"success": False, "error_type": "AUTH_ERROR", "detected": "LinkedIn captcha challenge detected.", "raw": "captcha", "post_url": None}
        if "two-step" in body or "verification code" in body or "2fa" in body:
            return {"success": False, "error_type": "AUTH_ERROR", "detected": "LinkedIn 2FA challenge detected.", "raw": "2fa", "post_url": None}
        if "security challenge" in body:
            return {"success": False, "error_type": "AUTH_ERROR", "detected": "LinkedIn security challenge detected.", "raw": "security_challenge", "post_url": None}
        return None

    def _navigate_to_post_surface(self, page):
        if self.post_target == "company":
            page.goto(self.company_page_url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2500)
            body = page.content().lower()
            if "you don't have access" in body or "request admin access" in body:
                return {
                    "success": False,
                    "error_type": "AUTH_ERROR",
                    "detected": "Missing LinkedIn company page admin access.",
                    "raw": "missing_company_admin_access",
                    "post_url": None,
                }
        else:
            page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(1500)
        return None

    def _open_composer(self, page):
        selectors = [
            'button[aria-label*="Start a post"]',
            'button:has-text("Start a post")',
            'span:has-text("Start a post")',
            'button:has-text("Create")',
        ]
        for sel in selectors:
            loc = page.locator(sel).first
            if loc.count() > 0:
                loc.click(timeout=5000)
                page.wait_for_timeout(1500)
                if page.locator('[role="dialog"]').count() > 0 or page.locator('div[contenteditable="true"]').count() > 0:
                    return None
        return {
            "success": False,
            "error_type": "PLATFORM_AUTOMATION_ERROR",
            "detected": "LinkedIn post composer could not be opened.",
            "raw": "composer_open_failed",
            "post_url": None,
        }

    def _fill_caption(self, page, caption):
        textbox = page.locator('div[role="textbox"][contenteditable="true"]').first
        if textbox.count() == 0:
            textbox = page.locator('div[contenteditable="true"]').first
        if textbox.count() == 0:
            return {
                "success": False,
                "error_type": "PLATFORM_AUTOMATION_ERROR",
                "detected": "LinkedIn caption textbox not found.",
                "raw": "missing_caption_textbox",
                "post_url": None,
            }
        textbox.click(timeout=5000)
        textbox.fill(caption)
        return None

    def _attach_media_if_any(self, page, media_files):
        if not media_files:
            return None
        file_input = page.locator('input[type="file"]').first
        if file_input.count() == 0:
            button = page.get_by_role("button", name="Add a photo")
            if button.count() == 0:
                button = page.get_by_role("button", name="Add a video")
            if button.count() > 0:
                button.first.click()
                page.wait_for_timeout(1000)
            file_input = page.locator('input[type="file"]').first

        if file_input.count() == 0:
            return {
                "success": False,
                "error_type": "MEDIA_ERROR",
                "detected": "LinkedIn media input not available in composer.",
                "raw": "missing_media_file_input",
                "post_url": None,
            }

        file_input.set_input_files(media_files)
        page.wait_for_timeout(4000)
        body = page.content().lower()
        if "couldn't upload" in body or "failed to upload" in body or "unsupported" in body:
            return {
                "success": False,
                "error_type": "MEDIA_ERROR",
                "detected": "LinkedIn rejected media upload.",
                "raw": "upload_rejected",
                "post_url": None,
            }
        return None

    def _publish_if_clear(self, page):
        publish = page.get_by_role("button", name="Post").first
        if publish.count() == 0:
            publish = page.get_by_role("button", name="Publish").first
        if publish.count() == 0:
            return {
                "success": False,
                "error_type": "PLATFORM_AUTOMATION_ERROR",
                "detected": "Publish button was not found in LinkedIn composer.",
                "raw": "missing_publish_button",
                "post_url": None,
            }
        if publish.is_disabled():
            return {
                "success": False,
                "error_type": "CONTENT_ERROR",
                "detected": "LinkedIn publish button is disabled; content may be invalid.",
                "raw": "publish_disabled",
                "post_url": None,
            }
        publish.click(timeout=5000)
        page.wait_for_timeout(3000)
        return None

    def _confirm_publish(self, page):
        url_now = page.url
        body = page.content().lower()
        post_url = None

        # Only signals that belong to this attempt count: landing on the new
        # post's permalink, or LinkedIn's success toast, whose link is the new
        # post. A post link anywhere else on the page can be any post in the
        # feed, and words like "shared" appear in ordinary feed text.
        path = _linkedin_path(url_now) or ""
        if path.startswith(("/feed/update/", "/posts/")):
            post_url = url_now

        success_toast = page.locator(
            '[role="status"]:has-text("Post successful"), '
            '[role="alert"]:has-text("Post successful"), '
            '[role="status"]:has-text("Your post is now live")'
        )
        toast_shown = success_toast.count() > 0

        if not post_url and toast_shown:
            toast_link = success_toast.first.locator('a[href*="/feed/update/"]')
            if toast_link.count() > 0:
                href = toast_link.first.get_attribute("href")
                if href:
                    post_url = href if href.startswith("http") else f"https://www.linkedin.com{href}"

        if post_url or toast_shown:
            # Passive Lead Scout Sweep
            try:
                if self.settings.get("lead_scout", {}).get("enabled", True):
                    from ..scout_logger import log_leads
                    leads = self.scout_leads(page, self.settings)
                    log_leads(leads)
            except Exception as e:
                print(f"[LinkedIn Scout] Error running Lead Scout: {e}")

            if post_url:
                return _result(True, post_url=post_url)
            else:
                return _result(
                    True,
                    post_url=None,
                    error_type=None,
                    detected="LinkedIn displayed an explicit post-success confirmation message.",
                    raw="explicit_success_toast_detected",
                )

        if "temporarily restricted" in body or "try again later" in body:
            return _result(False, error_type="RATE_LIMIT_ERROR", detected="LinkedIn temporarily restricted posting.", raw="rate_limited")

        return _result(
            False,
            error_type="PLATFORM_AUTOMATION_ERROR",
            detected="Publish confirmation was uncertain; no reliable success signal found.",
            raw="publish_confirmation_uncertain",
            screenshot_path=self._capture_screenshot(page),
        )

    def _capture_screenshot(self, page):
        if page is None:
            return None
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out_dir = LOGS_DIR / datetime.now().date().isoformat() / "failures"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"linkedin-{stamp}.png"
        try:
            page.screenshot(path=str(path), full_page=True)
            return path
        except Exception:
            return None

    def _classify_error(self, message):
        msg = message.lower()
        if "timeout" in msg or "net::" in msg or "connection" in msg:
            return "NETWORK_ERROR"
        if "rate" in msg or "too many" in msg or "try again later" in msg:
            return "RATE_LIMIT_ERROR"
        if "upload" in msg or "media" in msg or "file" in msg:
            return "MEDIA_ERROR"
        if "auth" in msg or "login" in msg or "password" in msg:
            return "AUTH_ERROR"
        if "content" in msg or "disabled" in msg:
            return "CONTENT_ERROR"
        return "PLATFORM_AUTOMATION_ERROR"
