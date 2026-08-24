"""Email alerting for ETL runs, via the Resend HTTP API.

Posts via `curl` (subprocess) rather than urllib: Resend rejects urllib's
default User-Agent, curl works fine.
"""
from __future__ import annotations

import json
import logging
import subprocess

import config

logger = logging.getLogger("alerting")

RESEND_API_URL = "https://api.resend.com/emails"
ALERT_FROM = "etl@send.perspiq.ca"
ALERT_TO = "yanis@perspiq.ca"

FAILURE_SUBJECT = "ETL Alert: lorchidee-etl failed"
SUCCESS_SUBJECT = "ETL lorchidee-etl — succès"


def _send_email(to: str, subject: str, body: str, *, context: str) -> None:
    """Best-effort: logs and returns rather than raising if RESEND_API_KEY
    isn't set or the Resend API call itself fails - an alerting problem
    should never mask or replace the ETL result it's reporting on."""
    if not config.RESEND_API_KEY:
        logger.warning("RESEND_API_KEY not set - skipping %s email", context)
        return

    payload = json.dumps(
        {
            "from": ALERT_FROM,
            "to": [to],
            "subject": subject,
            "text": body,
        }
    ).encode("utf-8")

    curl_cmd = [
        "curl", "-sS", "--max-time", "15",
        "-X", "POST", RESEND_API_URL,
        "-H", f"Authorization: Bearer {config.RESEND_API_KEY}",
        "-H", "Content-Type: application/json",
        "-d", "@-",
        "-w", "\n%{http_code}",
    ]
    try:
        result = subprocess.run(curl_cmd, input=payload, capture_output=True, timeout=20)
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.error("Failed to run curl for %s email: %s", context, exc)
        return

    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        logger.error("curl exited %s for %s email: %s", result.returncode, context, stderr)
        return

    stdout = result.stdout.decode("utf-8", errors="replace")
    resp_body, _, status_code = stdout.rpartition("\n")
    if not status_code.isdigit() or not (200 <= int(status_code) < 300):
        logger.error("Resend API returned HTTP %s for %s email: %s", status_code or "?", context, resp_body.strip())
        return

    logger.info("Sent %s email to %s", context, to)


def send_failure_alert(source: str, traceback_text: str) -> None:
    body = f"ETL source: {source}\n\n{traceback_text}"
    _send_email(ALERT_TO, FAILURE_SUBJECT, body, context=f"failure alert (source={source})")


def send_success_alert(summary: str) -> None:
    _send_email(ALERT_TO, SUCCESS_SUBJECT, summary, context="success notification")
