"""Runs all four ETL sources in sequence. One source failing does not stop the
others - each is isolated and logged to analytics.etl_run_log, and this
script exits non-zero if any source failed (so cron mail/alerting notices).
Each failure also sends an email alert via Resend (see alerting.py).
"""
import logging
import sys
import traceback

from alerting import send_failure_alert

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("run_all")

# source -> module that implements run(). "sheets" points at the one-off
# script (scripts/sheets_to_analytics.py) rather than etl/sheets_etl.py: it
# uses a plain API key against the public Sheet, which is what's actually
# configured and working right now, instead of the service account the
# other three sources need.
SOURCE_MODULES = {
    "bookings": "etl.bookings_etl",
    "ga4": "etl.ga4_etl",
    "gbp": "etl.gbp_etl",
    "sheets": "scripts.sheets_to_analytics",
}
SOURCES = list(SOURCE_MODULES)


def main() -> int:
    failures = []
    for source in SOURCES:
        logger.info("=== Running %s ETL ===", source)
        try:
            module = __import__(SOURCE_MODULES[source], fromlist=["run"])
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
