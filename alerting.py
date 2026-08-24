"""Email alerting for ETL runs, via the Resend HTTP API.

Uses urllib (stdlib) instead of adding `requests` as a dependency - this is
a single POST request, not worth a new package for.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

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

    request = urllib.request.Request(
        RESEND_API_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {config.RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as resp:
            resp.read()
        logger.info("Sent %s email to %s", context, to)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        logger.error("Resend API returned HTTP %s for %s email: %s", exc.code, context, detail)
    except urllib.error.URLError as exc:
        logger.error("Failed to reach Resend API for %s email: %s", context, exc)


def send_failure_alert(source: str, traceback_text: str) -> None:
    body = f"ETL source: {source}\n\n{traceback_text}"
    _send_email(ALERT_TO, FAILURE_SUBJECT, body, context=f"failure alert (source={source})")


def send_success_alert(summary: str) -> None:
    _send_email(ALERT_TO, SUCCESS_SUBJECT, summary, context="success notification")
