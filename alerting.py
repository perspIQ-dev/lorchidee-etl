"""Email alerting for ETL failures, via the Resend HTTP API.

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
ALERT_FROM = "lorchidee@send.lorchidee.ca"
ALERT_TO = "yanou.yadi@gmail.com"
ALERT_SUBJECT = "ETL Alert: lorchidee-etl failed"


def send_failure_alert(source: str, traceback_text: str) -> None:
    """Best-effort: logs and returns rather than raising if RESEND_API_KEY
    isn't set or the Resend API call itself fails, so an alerting problem
    never masks or replaces the underlying ETL failure being reported."""
    if not config.RESEND_API_KEY:
        logger.warning("RESEND_API_KEY not set - skipping failure email alert for source=%s", source)
        return

    body = f"ETL source: {source}\n\n{traceback_text}"
    payload = json.dumps(
        {
            "from": ALERT_FROM,
            "to": [ALERT_TO],
            "subject": ALERT_SUBJECT,
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
        logger.info("Sent failure alert email to %s for source=%s", ALERT_TO, source)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        logger.error("Resend API returned HTTP %s for source=%s alert: %s", exc.code, source, detail)
    except urllib.error.URLError as exc:
        logger.error("Failed to reach Resend API for source=%s alert: %s", source, exc)
