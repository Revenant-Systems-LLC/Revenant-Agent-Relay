import os
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parseaddr

from .reporter import subject_line


def send_email_report(run_log, settings, mode):
    """
    Send final email report after run completes.
    Returns structured status and never raises to the caller.
    """
    del mode  # mode retained for call compatibility

    email_config = settings.get("email")
    if not email_config:
        return {
            "attempted": False,
            "sent": False,
            "recipient": None,
            "subject": None,
            "error": None,
            "reason": "missing_config",
        }

    smtp_host = email_config.get("smtp_host")
    smtp_port = email_config.get("smtp_port", 587)
    sender_email_var = email_config.get("sender_email_env_var")
    sender_password_var = email_config.get("sender_password_env_var")
    recipient_email = email_config.get("recipient_email")
    subject_prefix = email_config.get("subject_prefix", "Revenant Relay")

    status = {
        "attempted": False,
        "sent": False,
        "recipient": recipient_email,
        "subject": None,
        "error": None,
        "reason": None,
    }

    if not smtp_host or not sender_email_var or not sender_password_var or not recipient_email:
        status["reason"] = "missing_config"
        status["error"] = "smtp host, sender env var names, and recipient are required"
        return status

    sender_email = os.getenv(sender_email_var)
    sender_password = os.getenv(sender_password_var)

    if not sender_email or not sender_password:
        status["reason"] = "missing_credentials"
        status["error"] = "sender credentials missing"
        return status

    if not _is_valid_email(recipient_email):
        status["reason"] = "missing_config"
        status["error"] = "invalid recipient email"
        return status

    try:
        subject = subject_line(run_log, prefix=subject_prefix)
        body = _build_report_body(run_log)

        msg = MIMEMultipart()
        msg["From"] = sender_email
        msg["To"] = recipient_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        status["attempted"] = True
        status["subject"] = subject

        context = ssl.create_default_context()
        if int(smtp_port) == 465:
            with smtplib.SMTP_SSL(smtp_host, smtp_port, context=context, timeout=30) as server:
                server.login(sender_email, sender_password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
                server.ehlo()
                server.starttls(context=context)
                server.ehlo()
                server.login(sender_email, sender_password)
                server.send_message(msg)

        print(f"[Email] Report sent to {recipient_email} (subject: {subject})")
        status["sent"] = True
        status["reason"] = "sent"
        return status
    except Exception as exc:
        print(f"[Email] Failed to send report: {exc}")
        status["error"] = str(exc)
        status["reason"] = "send_failed"
        return status


def _is_valid_email(value):
    if not value:
        return False
    parts = [p.strip() for p in value.split(",")]
    for part in parts:
        _, parsed = parseaddr(part)
        if not (parsed and "@" in parsed and "." in parsed.split("@")[-1]):
            return False
    return True


def _build_report_body(run_log):
    """Generate human-readable report body from run_log."""
    lines = []

    status = run_log.get("final_status", "UNKNOWN")
    successes = run_log.get("successes", 0)
    goal = run_log.get("daily_goal", 3)
    lines.append(f"Revenant Relay - {status} - {successes}/{goal} Posted")
    lines.append("")
    lines.append(f"Run ID: {run_log['run_id']}")
    lines.append(f"Date: {run_log['date']}  Mode: {run_log['mode']}")
    lines.append(f"Started: {run_log['started_at']}  Ended: {run_log.get('ended_at')}")
    lines.append(f"Stop reason: {run_log.get('stop_reason')}")
    lines.append(f"Successes: {successes}   Failures: {run_log.get('failures', 0)}")
    lines.append("")

    attempts = run_log.get("attempts", [])
    succeeded = [a for a in attempts if a.get("success")]
    failed = [a for a in attempts if not a.get("success")]

    if succeeded:
        lines.append("Successful posts:")
        for a in succeeded:
            lines.append(
                f"  - {a['platform']}  ad={a['ad_id']}  tier={a['tier']}  url={a.get('post_url')}"
            )
        lines.append("")

    if failed:
        lines.append("Failed attempts:")
        for a in failed:
            lines.append(f"  - {a['platform']}  ad={a['ad_id']}  tier={a['tier']}")
            lines.append(
                f"      type={a.get('error_type')}  cause={a.get('likely_cause')}"
            )
            lines.append(f"      fix={a.get('suggested_fix')}")
            if a.get("screenshot_path"):
                lines.append(f"      screenshot={a['screenshot_path']}")
        lines.append("")

    return "\n".join(lines)
