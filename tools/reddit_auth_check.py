"""Read-only Reddit auth check. Posts nothing.

Loads the DPAPI vault the same way Relay does, builds the same PRAW client the
adapter builds, and makes one read-only call (`reddit.user.me()`). Prints the
exact exception class and HTTP body on failure, because "it didn't work" is
what has cost this project months.

    py -3.11 tools/reddit_auth_check.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.secret_loader import load_secrets  # noqa: E402

PLACEHOLDERS = {"your_id", "your_secret", "your_token", "", None}


def main() -> int:
    load_secrets()

    import os

    settings = json.loads(Path("config/settings.json").read_text(encoding="utf-8"))
    cfg = settings.get("reddit", {})

    fields = {
        "client_id": os.getenv(cfg.get("client_id_env_var", ""), ""),
        "client_secret": os.getenv(cfg.get("client_secret_env_var", ""), ""),
        "refresh_token": os.getenv(cfg.get("refresh_token_env_var", ""), ""),
        "username": os.getenv(cfg.get("username_env_var", ""), ""),
        "password": os.getenv(cfg.get("password_env_var", ""), ""),
        "user_agent": os.getenv(cfg.get("user_agent_env_var", ""), ""),
    }

    print("=" * 68)
    print("CREDENTIAL STATE (values never printed except the two public ones)")
    print("=" * 68)
    for name in ("client_id", "client_secret", "refresh_token", "password"):
        v = fields[name]
        if v in PLACEHOLDERS:
            state = "PLACEHOLDER" if v else "ABSENT"
        else:
            state = f"SET (len={len(v)})"
        print(f"  {name:<16} {state}")
    print(f"  {'username':<16} {fields['username']!r}")
    print(f"  {'user_agent':<16} {fields['user_agent']!r}")
    print()

    blockers = []
    if fields["client_id"] in PLACEHOLDERS:
        blockers.append("client_id is a placeholder or absent")
    if fields["client_secret"] in PLACEHOLDERS:
        blockers.append("client_secret is a placeholder or absent")
    if not fields["username"] or "@" in fields["username"]:
        blockers.append("username must be the handle, not an email")
    if not fields["password"]:
        blockers.append("password absent")

    if blockers:
        print("BLOCKED — not attempting a live call:")
        for b in blockers:
            print(f"  - {b}")
        print()
        print("Reddit will not issue a token until the app's real client id and")
        print("secret are in the vault. Nothing else here is wrong.")
        return 2

    try:
        import praw
        import prawcore
    except ImportError as e:
        print(f"FAIL — praw not importable: {e}")
        return 3

    branch = "refresh_token" if fields["refresh_token"] not in PLACEHOLDERS else "username/password"
    print(f"Auth branch: {branch}")
    print(f"praw {praw.__version__} / prawcore {prawcore.__version__}")
    print()

    kwargs = {
        "client_id": fields["client_id"],
        "client_secret": fields["client_secret"],
        "user_agent": fields["user_agent"] or "RevenantRelay/1.0",
    }
    if branch == "refresh_token":
        kwargs["refresh_token"] = fields["refresh_token"]
    else:
        kwargs["username"] = fields["username"]
        kwargs["password"] = fields["password"]

    print("Calling reddit.user.me() — read-only, posts nothing...")
    try:
        reddit = praw.Reddit(**kwargs)
        me = reddit.user.me()
    except Exception as e:  # noqa: BLE001 — the whole point is to see anything
        print()
        print("FAIL")
        print(f"  exception class : {type(e).__module__}.{type(e).__name__}")
        print(f"  message         : {e}")
        response = getattr(e, "response", None)
        if response is not None:
            print(f"  http status     : {getattr(response, 'status_code', '?')}")
            try:
                print(f"  http body       : {response.text[:800]}")
            except Exception:
                pass
        print()
        _explain(e)
        return 1

    print()
    print("OK")
    print(f"  authenticated as : {me}")
    print(f"  link karma       : {getattr(me, 'link_karma', '?')}")
    print(f"  comment karma    : {getattr(me, 'comment_karma', '?')}")
    print(f"  account created  : {getattr(me, 'created_utc', '?')}")
    return 0


def _explain(exc: Exception) -> None:
    text = f"{type(exc).__name__}: {exc}".lower()
    if "invalid_grant" in text:
        print("  READ: invalid_grant means Reddit rejected the username/password pair.")
        print("        If the account has 2FA enabled, the password must be sent as")
        print("        'password:123456' with a current TOTP code — which cannot work")
        print("        unattended. In that case create a refresh token instead.")
    elif "401" in text or "unauthorized" in text:
        print("  READ: 401 points at the client id/secret pair, not the account.")
        print("        Confirm they came from the 'script' app on reddit.com/prefs/apps.")
    elif "403" in text:
        print("  READ: 403 means authentication worked but the action was refused.")
    elif "received 404" in text:
        print("  READ: 404 on the token endpoint usually means the app type is wrong —")
        print("        password auth requires a 'script' app.")
    else:
        print("  READ: unclassified. The exception class above is the thing to search.")


if __name__ == "__main__":
    raise SystemExit(main())
