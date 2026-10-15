import json
from datetime import date
from .paths import LOGS_DIR


def _log_dir_for_today():
    d = LOGS_DIR / date.today().isoformat()
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_run_json(run):
    path = _log_dir_for_today() / "run.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(run, f, indent=2)
    return path


def write_report_txt(run):
    path = _log_dir_for_today() / "report.txt"
    lines = []
    successes = run.get("successes", 0)
    lines.append(subject_line(run))
    lines.append("")
    lines.append(f"Run ID: {run['run_id']}")
    lines.append(f"Date: {run['date']}  Mode: {run['mode']}")
    lines.append(f"Started: {run['started_at']}  Ended: {run.get('ended_at')}")
    lines.append(f"Stop reason: {run.get('stop_reason')}")
    lines.append(f"Successes: {successes}   Failures: {run.get('failures', 0)}")
    sim_count = run.get("simulated_successes", 0)
    if sim_count:
        lines.append(f"  (of which {sim_count} simulated — no adapter loaded, nothing real was posted)")
    lines.append("")

    succeeded = [a for a in run["attempts"] if a["success"]]
    failed = [a for a in run["attempts"] if not a["success"]]

    if succeeded:
        lines.append("Successful posts:")
        for a in succeeded:
            tag = " [SIMULATED]" if a.get("simulated") else ""
            lines.append(f"  - {a['platform']}  ad={a['ad_id']}  tier={a['tier']}  url={a.get('post_url')}{tag}")
        lines.append("")

    if failed:
        lines.append("Failed attempts:")
        for a in failed:
            lines.append(f"  - {a['platform']}  ad={a['ad_id']}  tier={a['tier']}")
            lines.append(f"      type={a.get('error_type')}  cause={a.get('likely_cause')}")
            lines.append(f"      fix={a.get('suggested_fix')}")
            if a.get("screenshot_path"):
                lines.append(f"      screenshot={a['screenshot_path']}")
        lines.append("")

    with path.open("w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


def subject_line(run, prefix="Revenant Relay"):
    """Email/console/report subject.

    Names simulation outright so a dev run that fell back to the simulator can
    never read as "COMPLETE - 3/3 Posted". The posted count is real posts only.
    """
    successes = run.get("successes", 0)
    goal = run.get("daily_goal", 3)
    status = run.get("final_status", "UNKNOWN")
    simulated = run.get("simulated_successes", 0)
    real = successes - simulated
    if simulated and real == 0:
        return f"{prefix} - SIMULATED - 0/{goal} Posted ({simulated} simulated)"
    if simulated:
        return f"{prefix} - {status} - {real}/{goal} Posted ({simulated} simulated)"
    return f"{prefix} - {status} - {successes}/{goal} Posted"
