# WoopSocial as a design reference for Relay: 2026-10-14

Read-only research. Source: the WoopSocial OpenAPI 3.1 contract at
`https://api.woopsocial.com/v1/openapi.yaml` (87 KB, fetched 2026-10-14, 24 endpoints, 116 schemas),
their docs site, and Relay's own source on the `claude/fix/relay-ad-library-sim-flag` branch.
No code changed. Nothing committed.

**Decision this document serves (Dave, 2026-10-14):** WoopSocial is not a Relay replacement and
will not be used as a service. It is the same kind of toolset Relay is. Anything in its design that
Relay could use gets reverse-engineered into Relay. Everything else is noise.

**What I have and do not have.** I have their public API contract and Relay's code. I do not have
WoopSocial's runtime behaviour, so every "they do X" below means "their contract promises X."
Where a settled decision of Dave's shapes a recommendation, the source is `NEXT-SESSION-PROMPT.md`,
not my inference.

---

## 1. The one-paragraph read

WoopSocial's contract is a **calendar-driven fan-out scheduler**: one parent post, N platform
targets, each target carrying its own delivery lifecycle, external post id, error, and analytics.
Relay is a **rotating picker**: one ad to one platform per attempt, three a day, cooldowns, no queue.
Those are different machines and Relay's shape is the right one for "post my approved ads
unattended and leave me alone." So the scheduler, the media library, the webhooks, and the
multi-target post model are noise. What is worth taking is the **data discipline around a single
delivery**: validate before you post, record a lifecycle not a boolean, read the result back, and
keep operator-action failures out of the automatic retry path. Relay already has pieces of each.

---

## 2. Ranked build list

Order rests on the ad inventory I ran today (26 of 31 media paths resolve, 7 approved, 5 still
broken) and the August audit. Item 1 first is 80%. The order of 2 through 7 is judgment, 60%.

### 2.1 Validator that gates eligibility

| | |
|---|---|
| **Spec** | `POST /posts/validate` returns `isValid`, blocking `errors[]` and non-blocking `warnings[]`, each with a `path` and a `ValidationField` enum (`MEDIA`, `LINK`, `CONTENT`, `TITLE`, `SCHEDULE`, platform-specific). Creation is atomic: one bad target, no post. |
| **Relay today** | Each adapter checks caption and media inside `post_ad` (`facebook.py:110-134`, `instagram.py`). A bad ad is discovered at post time and eats a daily slot and a consecutive-failure count. `ad_selector.py:19-24` already does the right thing for one rule (Reddit ads with no `target_subreddits` are ineligible). Nothing checks the library up front, which is why "1 of 31 postable" had to be found by hand in August. |
| **Build** | `src/validate.py`: walk every `ad.json`, run per-platform checks, return errors and warnings per (ad, platform). Checks: `media_path` exists, is non-empty, is **relative** (every dead path in the audit was absolute); Instagram wants JPEG; Facebook under 4 MB; caption present and within platform length (X 280, Bluesky 300 graphemes, Instagram 2200, Reddit title 300); `url` present; `allowed_platforms` non-empty; Reddit needs `target_subreddits`. `ad_selector` excludes any (ad, platform) with errors. `main.py` runs it once at start and puts the summary in the report. A `python -m src.validate` entry prints the inventory. **It checks presence and length only. It never reads or judges caption content.** |
| **Files** | new `src/validate.py`, `src/ad_selector.py`, `src/main.py`, `src/reporter.py`, new test |
| **Size** | ~150 lines plus tests |
| **Why first** | It turns the August audit into a command Relay runs itself, and it stops the run loop from spending the day on ads that cannot post. |

### 2.2 Delivery lifecycle with an UNCONFIRMED state

| | |
|---|---|
| **Spec** | Every target carries `deliveryStatus` (`NOT_STARTED`, `SENDING`, `PUBLISHED`, `FAILED`), `externalPostId`, `externalPostUrl`, `errorMessage`, `deliveryCompletedAt`. Their own retry text admits the hole: "retries may create duplicates if a platform published but the response was lost." |
| **Relay today** | The attempt record in `main.py:154-170` has `success` (bool), `post_url`, and the `simulated` flag added in August. Relay's error side is **richer** than the spec: `error_type`, `detected_issue`, `likely_cause`, `suggested_fix`, `screenshot_path`, `transient`. Keep all of it. What is missing is a status that distinguishes "the click fired" from "I read the permalink back." |
| **Build** | Add `delivery_status` and `external_post_id` to the attempt record. `PUBLISHED` only when the adapter read the resulting id or permalink back (Facebook and Bluesky already return ids; the Reddit browser adapter must read the permalink, which is step 4 of `NEXT-SESSION-PROMPT.md`). If the post action ran but read-back failed: `UNCONFIRMED`. Unconfirmed writes **no cooldown, no history success, and blocks retry** of that ad on that platform until a human looks. That closes the duplicate hole the spec concedes. |
| **Files** | `src/main.py`, each adapter's `_result()`, `src/history.py`, `src/reporter.py` |
| **Size** | ~60 lines across files, mostly plumbing |
| **Note** | This supports the Reddit browser plan rather than reopening it. A browser adapter that cannot read its permalink back reports UNCONFIRMED, not success. Relay has been burned twice by fake successes; this is the structural fix behind the `simulated` flag. |

### 2.3 Preflight connection check

| | |
|---|---|
| **Spec** | `SocialAccount.status` is `CONNECTED` or `DISCONNECTED`; `GET /social-accounts/{id}/platform-inputs` returns what the account can currently do (Pinterest boards, TikTok privacy levels, max video length). |
| **Relay today** | `reddit_auth_check.py` and `relay_login.py --check` exist as loose root scripts. The run loop learns a platform is dead only when `load_adapter` returns `None` (`adapter_loader.py`), after it has already been selected. |
| **Build** | `src/preflight.py`: for every enabled platform, confirm credentials present and the session or token answers (Graph API `GET /me` for Facebook and Instagram, Bluesky session create, browser profile cookie present for Reddit). Result per platform: `CONNECTED` / `DISCONNECTED` / `UNKNOWN` plus reason. `build_tiers` drops `DISCONNECTED` platforms for the day. Report lists them. |
| **Files** | new `src/preflight.py`, `src/platform_selector.py`, `src/main.py`, `src/reporter.py` |
| **Size** | ~100 lines |

### 2.4 Operator-action failures leave the retry tiers

| | |
|---|---|
| **Spec** | `PostRetryPolicy` retries failed targets but **not** "known failures requiring user intervention, such as insufficient credits or disconnected accounts." |
| **Relay today** | `error_type` already names the operator-action classes (`AUTH_ERROR`, `MEDIA_ERROR`, `CONTENT_ERROR`). But `platform_selector.build_tiers` puts every platform that failed yesterday into tier 2 regardless of why. An expired token yesterday is an expired token today: it burns a slot and a consecutive-failure count, and three of those stop the run. |
| **Build** | `history.platforms_used_yesterday` returns the last `error_type` per failed platform. `build_tiers` puts `AUTH_ERROR` and `MEDIA_ERROR` platforms into a new `needs_operator` list instead of tier 2. Report names them. They come back the day after a successful preflight (2.3). |
| **Files** | `src/history.py`, `src/platform_selector.py`, `src/reporter.py` |
| **Size** | ~30 lines |

### 2.5 Per-platform media override, relative paths only

| | |
|---|---|
| **Spec** | `SocialAccountInputBase.contentOverride` lets one target carry different text **and different media** from the parent post. |
| **Relay today** | `platform_captions` covers text. One `media_path` serves every platform. Instagram accepts JPEG only and fetches it from a public URL; every ad is PNG or MP4. |
| **Build** | Optional `platform_media: {"instagram": "ads/skills/ALG-001/media.jpg"}` in `ad.json`, read by `ad_loader` the same way `caption_for` reads `platform_captions`. Validator (2.1) rejects absolute paths everywhere. This removes the need for an automatic conversion pipeline: Dave drops a JPEG in the folder once. The public-hosting half of Instagram's requirement is unchanged and is listed under open questions. |
| **Files** | `src/ad_loader.py`, `src/platforms/instagram.py`, `src/validate.py` |
| **Size** | ~20 lines |

### 2.6 Deterministic test platform

| | |
|---|---|
| **Spec** | `WOOPTEST` is a platform value that "goes through the full scheduling pipeline but is never published." `shouldSucceed: false` forces a failure. |
| **Relay today** | `simulator.py` rolls dice against `simulated_success_rate`. Tests of the run loop cannot assert an outcome without patching `random`. |
| **Build** | `src/platforms/test.py`: an adapter whose outcome is fixed by `RR_TEST_OUTCOME` (`success`, `fail_transient`, `fail_auth`, `unconfirmed`). Registered in `adapter_loader`, enabled only in dev mode, always reports `simulated: true`. Lets the loop, cooldown, history, and report paths be tested end to end with known results. |
| **Files** | new `src/platforms/test.py`, `src/adapter_loader.py`, `config/platforms.json`, tests |
| **Size** | ~60 lines |
| **Note** | Low urgency. The real test is still the first live post to `r/u_DeadOfTheDave` named in `NEXT-SESSION-PROMPT.md`. |

### 2.7 Click measurement (proposal, touches the `url` field)

| | |
|---|---|
| **Spec** | `PostAnalytics` stores daily lifetime snapshots per target: views, impressions, likes, comments, shares, saves, **clicks**, engagement rate, each with `collectedAt`. |
| **Relay today** | Nothing. And Dave's actual question is whether anyone clicks, not whether the bot posted. |
| **Build** | Not platform analytics; Reddit has no API path for this account and the others each need their own insights call. Instead: UTM parameters on the destination link per ad per platform (`?utm_source=reddit&utm_medium=relay&utm_campaign=ALG-001`), appended by `caption_for` at post time from the `url` field, never stored in the caption. PostHog on revenantsystems.net reads them. Muninn records roughly $40k of unused PostHog credit. Zero platform dependency, answers the only question that matters. The attempt record from 2.2 (external id, URL, timestamp) is the join key if platform-side metrics are ever collected later. |
| **Files** | `src/ad_loader.py` (append at post time), site-side PostHog snippet |
| **Size** | ~15 lines in Relay |
| **Flag** | This changes the link that goes out under Dave's name. It is not copy, but it is his field. Proposal, not plan. |

---

## 3. Not adopting

| Spec feature | Why not |
|---|---|
| `DRAFT` / `PUBLISH_NOW` / `SCHEDULE_FOR_LATER` calendar | Relay is a rotating picker by design; `approved: false` is already "draft." A calendar is a thing Dave would have to fill. |
| Parent post fanned out to N accounts, atomic | Relay posts one ad to one platform with its own caption. Fan-out would post the same ad everywhere on the same day, which the cooldown and tier design exists to prevent. |
| Media library, chunked upload sessions, presigned URLs, 5 GB cap | Files in ad folders are the library. Chunked upload solves a multi-tenant SaaS problem. |
| `autoDeleteMediaAfterPublish` | SaaS storage billing concern. |
| Webhooks with signing secrets | Relay reports once per run. The actual reporting gap is in section 4, and it is not a webhook. |
| Projects / business profiles | One organisation. |
| OAuth authorization URL flow | Each Relay adapter authenticates its own way; Facebook and Instagram use a Page token, Reddit uses a logged-in browser profile. |
| TikTok, YouTube, Pinterest, Threads field schemas | Not in Relay's platform set per `config/platforms.json` and `NEXT-SESSION-PROMPT.md`. TikTok's six required booleans are a good illustration of why. |
| `retryPolicy` with 1 to 5 retries and 60 s to 24 h delay | Relay already retries transient failures once in-run and re-tiers next day. Borrow the exclusion rule only (2.4). |
| Concurrency limit, `429`, `Retry-After` | Relay makes three posts a day. |
| The MCP server | No LLM at runtime is a Relay law (README, `NEXT-SESSION-PROMPT.md` decision 3). |
| Analytics collection from platforms | Needs per-platform insight APIs. UTM plus PostHog (2.7) answers the question without them. |

---

## 4. What Relay claims versus what exists (found while mapping)

- **Discord reporting has no code.** `config/settings.json` sets `send_discord_report: true` in scheduled mode, and `README.md` says reports land in `skalds-log` and ads live in `mimirs-well`. `grep -ri discord src/` returns nothing. `main.py` calls only the emailer. The email path itself cannot send until `RR_EMAIL` and `RR_EMAIL_PASSWORD` exist in the vault (per `HANDOFF.md`). So today a scheduled run reports to nobody. This outranks everything in section 3 about webhooks: the gap is not event delivery, it is that one of the two configured channels was never built and the other has no credentials.
- **The README platform list is stale.** It names Indeed and omits Bluesky; `config/platforms.json` has the reverse.
- **`CLAUDE.md` still names `B:\secrets\revenant-relay.env`** as the secret path. The live path is `A:\env\revenant-relay.dpapi` (HANDOFF, 2026-08-06).

---

## 5. Open questions, Dave's to answer

1. **Instagram public hosting.** The adapter needs the JPEG reachable over HTTPS. `NEXT-SESSION-PROMPT.md` says revenantsystems.net can host. How do files get to cPanel `public_html/assets/` today: manual upload, FTP, something scripted? The answer decides whether 2.5 is enough or a publish step is needed.
2. **UTM on the `url` field (2.7).** Yes or no. If yes, PostHog goes on the site.
3. **Discord or email.** Which report channel is the one you will read? Building Discord is new code; email needs a Gmail app password in the vault via `secret_gui.py`.
4. **RTS-005 media.** Folder holds `auto-match.gif`, `Gunmetal_theme2.png`, `Ruby_Theme2.png`; `media_path` is empty. Your pick.
5. **SEC-001 art.** Approved, no media file at all.
6. **RH-003, BUN-005, SYS-003** still point at dead `C:\The-Ossuary` paths. Source files exist on B: (`assets/motto/RSLLC_Quote1.png`, `assets/deprecated/Circle4.png`; both verified identical to the website copies by hash). Copy into the ad folders and repoint on your word.

---

## 6. Confidence

- Validator first: 80%, one step from today's inventory run and the August audit.
- Lifecycle with UNCONFIRMED as the fake-success fix: 80%, grounded in the spec's own duplicate admission and Relay's two prior fake-success incidents.
- Order of 2.3 through 2.7: 60%, judgment.
- Discord has no code: 95%, grep this session.
- Anything about WoopSocial's runtime behaviour: not checked; the contract is all I read.
