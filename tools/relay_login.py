"""One-time manual login for browser-driven adapters.

Opens a real, visible browser using Relay's persistent profile for a platform.
You log in by hand, close the window, and the session cookies stay on disk.
Every run after that is already authenticated.

This exists so no adapter ever types a password. A human does the login once,
including any captcha or 2FA, and the bot inherits the result.

    py -3.11 tools/relay_login.py reddit          # log in
    py -3.11 tools/relay_login.py reddit --check  # is the session still good?
    py -3.11 tools/relay_login.py reddit --reset  # wipe and start over

Close the browser window when you are done. The script waits for it.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.browser_session import clear_profile, has_profile, launch_persistent, profile_dir  # noqa: E402

# Where to land for the login, and how to tell afterwards that it worked.
# The check URL must be a page that renders differently for a logged-in user,
# and the marker must be something only a signed-in session ever shows.
PLATFORMS = {
    "reddit": {
        "login_url": "https://old.reddit.com/login",
        "check_url": "https://old.reddit.com/",
        "logged_in_selector": "form.logout",
        "note": "Old Reddit interface. Same account and same posts as normal Reddit.",
    },
}


def _usage() -> int:
    print(__doc__)
    print("Known platforms: " + ", ".join(sorted(PLATFORMS)))
    return 2


def check(platform: str) -> int:
    cfg = PLATFORMS[platform]
    if not has_profile(platform):
        print(f"No profile yet for {platform}. Run: py -3.11 tools/relay_login.py {platform}")
        return 1

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        context = launch_persistent(p, platform, headless=True)
        try:
            page = context.new_page()
            page.goto(cfg["check_url"], wait_until="domcontentloaded")
            ok = page.locator(cfg["logged_in_selector"]).count() > 0
        finally:
            context.close()

    if ok:
        print(f"{platform}: session is live.")
        return 0
    print(f"{platform}: NOT logged in. Run: py -3.11 tools/relay_login.py {platform}")
    return 1


def login(platform: str) -> int:
    cfg = PLATFORMS[platform]
    print(f"Opening a browser for {platform}.")
    if cfg.get("note"):
        print(f"  {cfg['note']}")
    print(f"  Profile: {profile_dir(platform)}")
    print()
    print("Log in, then CLOSE THE BROWSER WINDOW. This script waits for that.")
    print("Do not log out afterwards; the saved session is what Relay uses.")
    print()

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        context = launch_persistent(p, platform, headless=False)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(cfg["login_url"], wait_until="domcontentloaded")

        closed = {"done": False}
        context.on("close", lambda _: closed.__setitem__("done", True))

        # Block until the human closes the window. Nothing is automated here.
        try:
            while not closed["done"]:
                page.wait_for_timeout(500)
        except Exception:
            pass  # window closed mid-poll, which is the expected exit

    print()
    return check(platform)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:]]
    if not args or args[0] not in PLATFORMS:
        raise SystemExit(_usage())

    name = args[0]
    flags = set(args[1:])

    if "--reset" in flags:
        clear_profile(name)
        print(f"Cleared {name} profile.")
        raise SystemExit(login(name))
    if "--check" in flags:
        raise SystemExit(check(name))
    raise SystemExit(login(name))
