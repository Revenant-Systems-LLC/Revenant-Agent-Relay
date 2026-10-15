"""
PRAW-based Reddit adapter. Cannot go live: Reddit will not let this account
create an API app (the "create app" form never fires a request — see
NEXT-SESSION-PROMPT.md, "Reddit: the API path is dead, the browser path is
open"). client_id/client_secret can never be populated, so RedditAdapter()
always raises and adapter_loader falls back to the simulator.

Superseded by a browser-automation adapter (src/platforms/reddit_browser.py,
not yet built) driving a real logged-in session. Left in place as reference/
in case Reddit ever opens API access; not wired to anything once the browser
adapter lands.
"""
import logging
import os
import random
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    import praw
    import prawcore
    PRAW_AVAILABLE = True
except ImportError:
    PRAW_AVAILABLE = False


def _resolve_media(media_path):
    """Resolve media_path relative to repo root if not absolute."""
    p = Path(media_path)
    if p.is_absolute():
        return p
    from ..paths import ROOT
    return ROOT / p


_ERROR_CAUSE = {
    "AUTH_ERROR": "Invalid credentials or Reddit token expired.",
    "CONTENT_ERROR": "Post removed or rejected — likely violates subreddit rules.",
    "RATE_LIMIT_ERROR": "Reddit is throttling requests (429).",
    "PLATFORM_AUTOMATION_ERROR": "Subreddit does not exist or posting not permitted.",
    "NETWORK_ERROR": "Transient network or connection failure.",
    "UNKNOWN_ERROR": "Could not classify failure reliably.",
}

_ERROR_FIX = {
    "AUTH_ERROR": "Re-authenticate: refresh Reddit API tokens.",
    "CONTENT_ERROR": "Review subreddit rules; adjust caption or target a different subreddit.",
    "RATE_LIMIT_ERROR": "Skip Reddit for today; retry tomorrow.",
    "PLATFORM_AUTOMATION_ERROR": "Verify subreddit name and that the account has posting rights.",
    "NETWORK_ERROR": "Retry once; likely transient.",
    "UNKNOWN_ERROR": "Investigate manually.",
}

_TRANSIENT_TYPES = {"NETWORK_ERROR"}


def _result(success, post_url=None, error_type=None, detected=None, raw=None):
    return {
        "success": success,
        "error_type": error_type,
        "detected_issue": detected,
        "likely_cause": _ERROR_CAUSE.get(error_type) if error_type else None,
        "suggested_fix": _ERROR_FIX.get(error_type) if error_type else None,
        "raw_error": raw,
        "post_url": post_url,
        "screenshot_path": None,
        "transient": error_type in _TRANSIENT_TYPES if error_type else False,
    }


class RedditAdapter:
    def __init__(self, reddit_config, settings=None):
        """
        reddit_config: dict from settings["reddit"] with env var names.
        Raises RuntimeError if PRAW not installed or credentials missing.
        """
        self.settings = settings or {}
        if not PRAW_AVAILABLE:
            raise RuntimeError(
                "praw is not installed. Run: pip install praw"
            )

        cfg = reddit_config or {}
        client_id = os.getenv(cfg.get("client_id_env_var", ""))
        client_secret = os.getenv(cfg.get("client_secret_env_var", ""))
        refresh_token = os.getenv(cfg.get("refresh_token_env_var", ""))
        username = os.getenv(cfg.get("username_env_var", ""))
        password = os.getenv(cfg.get("password_env_var", ""))
        user_agent = os.getenv(cfg.get("user_agent_env_var", "")) or "RevenantRelay/1.0"

        if not client_id or not client_secret:
            raise RuntimeError(
                "Reddit credentials missing. Set env vars: "
                f"{cfg.get('client_id_env_var')}, {cfg.get('client_secret_env_var')}"
            )

        if refresh_token:
            self._reddit = praw.Reddit(
                client_id=client_id,
                client_secret=client_secret,
                refresh_token=refresh_token,
                user_agent=user_agent,
            )
        elif username and password:
            self._reddit = praw.Reddit(
                client_id=client_id,
                client_secret=client_secret,
                username=username,
                password=password,
                user_agent=user_agent,
            )
        else:
            raise RuntimeError(
                "Reddit auth incomplete. Provide either a refresh token "
                f"({cfg.get('refresh_token_env_var')}) or username/password "
                f"({cfg.get('username_env_var')}, {cfg.get('password_env_var')})."
            )

    def select_subreddit(self, ad, cooldown_buckets):
        """
        Pick a subreddit from ad["target_subreddits"] that isn't on cooldown.
        Returns subreddit name (str) or None if all are on cooldown.
        """
        candidates = ad.get("target_subreddits", [])
        if not candidates:
            return None

        reddit_cooldowns = cooldown_buckets.get("reddit", {})
        eligible = [
            sub for sub in candidates
            if ad["id"] not in reddit_cooldowns.get(sub, {})
        ]
        if not eligible:
            return None

        return random.choice(eligible)

    def post_ad(self, ad, caption, subreddit):
        """
        Post ad to subreddit. Returns result dict matching simulator contract.
        Supports image posts (post_format == "image") and link posts.
        """
        post_format = ad.get("post_format", "link")
        title = caption[:300]  # Reddit title limit

        try:
            sub = self._reddit.subreddit(subreddit.removeprefix("r/"))

            if post_format == "image":
                media_files = []
                paths = ad.get("media_paths", [])
                if ad.get("media_path"):
                    paths.insert(0, ad["media_path"])
                    
                for path_str in paths:
                    resolved = _resolve_media(path_str)
                    if not resolved or not resolved.exists():
                        return _result(
                            False,
                            error_type="MEDIA_ERROR",
                            detected=f"Media file not found: {path_str}",
                            raw=f"FileNotFoundError: {path_str}",
                        )
                    media_files.append(str(resolved))
                
                if not media_files:
                    return _result(
                        False,
                        error_type="MEDIA_ERROR",
                        detected="No media files provided for image post format.",
                        raw="NoMediaError",
                    )
                
                if len(media_files) == 1:
                    submission = sub.submit_image(title=title, image_path=media_files[0])
                else:
                    gallery_images = [{"image_path": path} for path in media_files]
                    submission = sub.submit_gallery(title=title, images=gallery_images)
            else:
                url = ad.get("url", "")
                submission = sub.submit(title=title, url=url)

            post_url = f"https://www.reddit.com{submission.permalink}"

            # Passive Lead Scout Sweep
            try:
                if self.settings.get("lead_scout", {}).get("enabled", True):
                    from ..scout_logger import log_leads
                    leads = self.scout_leads(self.settings)
                    log_leads(leads)
            except Exception as e:
                print(f"[Reddit Scout] Error running Lead Scout: {e}")

            return _result(True, post_url=post_url)

        except Exception as e:
            return _classify_error(e)

    def scout_leads(self, settings):
        """
        Scan Reddit globally for keywords defined in settings.
        Returns a list of discovered lead dictionaries.
        """
        scout_cfg = settings.get("lead_scout", {})
        if not scout_cfg.get("enabled", True):
            return []

        keywords = scout_cfg.get("keywords", [])
        limit = scout_cfg.get("max_leads_per_run", 5)
        leads = []

        print(f"[Reddit Scout] Starting passive sweep for {len(keywords)} keywords...")
        try:
            our_name = ""
            try:
                our_name = self._reddit.user.me().name
            except Exception:
                logger.debug("Could not fetch Reddit username for dedup check", exc_info=True)

            for kw in keywords:
                results = self._reddit.subreddit("all").search(
                    query=kw,
                    limit=limit,
                    sort="new",
                    syntax="plain"
                )
                for submission in results:
                    author = str(submission.author) if submission.author else "[deleted]"
                    if our_name and author.lower() == our_name.lower():
                        continue
                    
                    content = submission.selftext[:500] if submission.is_self else f"[Link/Media Title: {submission.title}]"
                    permalink = f"https://www.reddit.com{submission.permalink}"
                    
                    leads.append({
                        "platform": "reddit",
                        "keyword": kw,
                        "author": author,
                        "post_content": content,
                        "post_url": permalink
                    })
        except Exception as e:
            print(f"[Reddit Scout] Warning: Passive sweep encountered an issue: {e}")
        
        return leads


def _classify_error(exc):
    err_str = str(exc)
    err_type = exc.__class__.__name__ if hasattr(exc, "__class__") else "Exception"

    if not PRAW_AVAILABLE:
        return _result(False, error_type="UNKNOWN_ERROR", detected=err_str, raw=err_str)

    # Auth failures
    if isinstance(exc, prawcore.exceptions.OAuthException):
        return _result(False, error_type="AUTH_ERROR",
                       detected="OAuth authentication failed.", raw=err_str)
    if isinstance(exc, prawcore.exceptions.Forbidden):
        return _result(False, error_type="AUTH_ERROR",
                       detected="Access forbidden — account or token issue.", raw=err_str)

    # Rate limiting
    if isinstance(exc, prawcore.exceptions.TooManyRequests):
        return _result(False, error_type="RATE_LIMIT_ERROR",
                       detected="Reddit rate limit hit (429).", raw=err_str)

    # Subreddit doesn't exist or no access
    if isinstance(exc, (prawcore.exceptions.NotFound, prawcore.exceptions.Redirect)):
        return _result(False, error_type="PLATFORM_AUTOMATION_ERROR",
                       detected="Subreddit not found or access denied.", raw=err_str)

    # Network/transient
    if isinstance(exc, prawcore.exceptions.RequestException):
        return _result(False, error_type="NETWORK_ERROR",
                       detected="Network failure contacting Reddit API.", raw=err_str)

    # praw APIException — content/rule violations
    if PRAW_AVAILABLE and isinstance(exc, praw.exceptions.APIException):
        return _result(False, error_type="CONTENT_ERROR",
                       detected=f"Reddit API rejected post: {err_str}", raw=err_str)

    return _result(False, error_type="UNKNOWN_ERROR",
                   detected=f"Unclassified error: {err_type}", raw=err_str)
