import json
import uuid
from datetime import date, datetime
from .paths import RUN_HISTORY_FILE


def _load():
    try:
        with RUN_HISTORY_FILE.open("r", encoding="utf-8") as f:
            return json.load(f).get("runs", [])
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return []


def _save(runs):
    with RUN_HISTORY_FILE.open("w", encoding="utf-8") as f:
        json.dump({"runs": runs}, f, indent=2)


def new_run(mode):
    return {
        "run_id": str(uuid.uuid4()),
        "date": date.today().isoformat(),
        "mode": mode,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "ended_at": None,
        "final_status": None,
        "stop_reason": None,
        "attempts": [],
        "successes": 0,
        "failures": 0,
    }


def append_run(run):
    runs = _load()
    runs.append(run)
    _save(runs)


def last_run():
    runs = _load()
    return runs[-1] if runs else None


def yesterdays_run():
    """Most recent run dated strictly before today. None if none."""
    today = date.today().isoformat()
    runs = _load()
    for run in reversed(runs):
        if run.get("date") and run["date"] < today:
            return run
    return None


def platforms_used_yesterday():
    """Return (succeeded, failed, attempted) sets from the most recent prior-day run.

    Simulated attempts are excluded entirely (not attempted/succeeded/failed) —
    a dev-mode run that fell back to the simulator must not shift today's tier
    selection as if something real happened.
    """
    y = yesterdays_run()
    succeeded, failed, attempted = set(), set(), set()
    if not y:
        return succeeded, failed, attempted
    for a in y.get("attempts", []):
        if a.get("simulated"):
            continue
        platform = a.get("platform")
        if not platform:
            continue
        attempted.add(platform)
        if a.get("success"):
            succeeded.add(platform)
        else:
            failed.add(platform)
    failed -= succeeded
    return succeeded, failed, attempted


def successful_ad_on_platform_yesterday(platform):
    """Which ad_id, if any, really succeeded on this platform in yesterday's run.

    Simulated successes don't count — see platforms_used_yesterday().
    """
    y = yesterdays_run()
    if not y:
        return None
    for a in y.get("attempts", []):
        if a.get("simulated"):
            continue
        if a.get("platform") == platform and a.get("success"):
            return a.get("ad_id")
    return None
