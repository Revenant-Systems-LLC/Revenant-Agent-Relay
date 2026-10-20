# Revenant Relay

Ad distribution bot for Revenant Systems LLC.

## What it is
Posts human-approved ads across selected social platforms on a rotating schedule. A self-hosted, free replacement for a paid scheduling service. It runs on your own machine.

## What it is not
- Not an ad generator. No AI-generated public-facing copy. Ever.
- Not an agent. No LLM at runtime. No persona. No reasoning loop.
- Not a social platform. Not a content engine. Not a growth suite.

## Architecture
- **Runtime:** pure Python. No agent framework.
- **State:** JSON files on disk in `data/`.
- **Operator surface:** Discord. Ads live in `mimirs-well`. Reports land in `skalds-log`.
- **Browser automation:** Playwright (added after engine works).
- **Schedule:** Windows Task Scheduler triggers once daily.

## Platforms (v1 target)
Facebook, Instagram, X, Reddit, Pinterest, TikTok, LinkedIn, Indeed.

## Daily goal
3 successful posts per scheduled run. Stops at 3 successes, 3 consecutive failures, or no eligible platform/ad combinations.

## Run modes
- **dev** — visible browser, local report, email/Discord optional.
- **scheduled** — headless, always reports to Discord, screenshots on failure.

## Project layout
```
Revenant-Relay/
  ads/         approved ad library (image or video + ad.json per ad)
  assets/      brand images referenced by ads
  config/      platforms.json, settings.json
  data/        run_history.json, platform_cooldowns.json, disabled_platforms.json (gitignored)
  docs/        platform permission matrix; docs/notes/ holds audits and session notes
  logs/        YYYY-MM-DD/ run.json, report.txt, failures/*.png (gitignored)
  src/         engine code
    platforms/ one adapter per platform
  tests/       pytest suite
  tools/       operator scripts: manual login, credential vault, auth checks
```

## Status
Core engine is implemented and runnable from local JSON config/state files.

## Installation
```
python -m pip install -r requirements.txt
python -m playwright install
```

## Operator tools
- `tools/relay_login.py <platform>` opens a real, visible browser so you log in once by hand. The session cookies stay on disk and every later run is already authenticated. `--check` tests the session, `--reset` wipes it. No adapter ever types a password.
- `tools/secret_tool.py` reads and edits the DPAPI-encrypted credential vault without writing plaintext to disk. `tools/secret_gui.py` is the same thing with a window.
- `tools/reddit_auth_check.py` makes one read-only Reddit call to prove the stored credentials work. Posts nothing.

## Tests
```
python -m pytest
```

## Platform credentials
Credentials are read from the encrypted vault or from the environment. Each adapter expects the variables below.

### Reddit
Required for Reddit adapter initialization and posting:
- `RR_REDDIT_CLIENT_ID`
- `RR_REDDIT_CLIENT_SECRET`
- `RR_REDDIT_USERNAME`
- `RR_REDDIT_PASSWORD`
- `RR_REDDIT_USER_AGENT`
- Optional: `RR_REDDIT_REFRESH_TOKEN`

### Pinterest
Required for real Pinterest posting:
- `RR_PINTEREST_USERNAME`
- `RR_PINTEREST_PASSWORD`
- Optional: `RR_PINTEREST_BOARD_NAME`

### TikTok
Required for TikTok adapter initialization:
- `RR_TIKTOK_USERNAME`
- `RR_TIKTOK_PASSWORD`
- Optional safety gate: `RR_TIKTOK_ENABLE_AUTOMATION` (default disabled; set to `true` to attempt browser automation bootstrap)

The adapter is upload prep only. It logs in, uploads the video and fills the caption, then stops without clicking Post. Every attempt is reported as `UPLOAD_PREP_ONLY` and never counts as a post.

### Snapchat
Required for Snapchat adapter initialization:
- `RR_SNAPCHAT_USERNAME`
- `RR_SNAPCHAT_PASSWORD`
- Optional: `RR_SNAPCHAT_POST_TARGET` (destination URL used in ad composer fields when present)
- Optional: `RR_SNAPCHAT_BUSINESS_URL` (defaults to `https://ads.snapchat.com`)

The adapter targets Snapchat Ads Manager campaign creation/save/publish controls when detectable. It does **not** claim reliable personal-story posting from Snapchat web account flow.

### LinkedIn
Required for real LinkedIn posting:
- `RR_LINKEDIN_USERNAME`
- `RR_LINKEDIN_PASSWORD`
- `RR_LINKEDIN_POST_TARGET` (`company` or `profile`; `company` recommended)
- `RR_LINKEDIN_COMPANY_PAGE_URL` (required when target is `company`)

### X
Required for real X posting:
- `RR_X_USERNAME`
- `RR_X_PASSWORD`

## Docs
- [Platform permission matrix](docs/platform-permission-matrix.md): what each platform's API and terms allow for automated posting.
- [docs/notes/](docs/notes/): readiness audit, task log, and session notes.

## License
Copyright Revenant Systems LLC. All rights reserved.
