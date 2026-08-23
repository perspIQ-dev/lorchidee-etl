"""Runs all four ETL sources in sequence. One source failing does not stop the
others - each is isolated and logged to analytics.etl_run_log, and this
script exits non-zero if any source failed (so cron mail/alerting notices).
Each failure also sends an email alert via Resend (see alerting.py).
"""
import logging
import sys
import traceback

import db
from alerting import send_failure_alert

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("run_all")

SOURCES = ["bookings", "ga4", "gbp", "sheets"]


def main() -> int:
    db.apply_schema()

    failures = []
    for source in SOURCES:
        logger.info("=== Running %s ETL ===", source)
        try:
            module = __import__(f"etl.{source}_etl", fromlist=["run"])
            module.run()
        except Exception:  # noqa: BLE001 - keep going, report at the end
            tb = traceback.format_exc()
            logger.exception("%s ETL failed", source)
            send_failure_alert(source, tb)
            failures.append(source)

    if failures:
        logger.error("ETL run completed with failures: %s", failures)
        return 1

    logger.info("ETL run completed successfully for all sources")
    return 0


if __name__ == "__main__":
    sys.exit(main())
