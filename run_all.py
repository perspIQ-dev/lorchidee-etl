"""Runs all four ETL sources in sequence. One source failing does not stop the
others - each is isolated and logged to analytics.etl_run_log, and this
script exits non-zero if any source failed (so cron mail/alerting notices).
Each failure sends an email alert via Resend; a fully successful run sends a
brief success notification instead (see alerting.py).
"""
import logging
import sys
import traceback
from datetime import datetime

from alerting import send_failure_alert, send_success_alert

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


def build_success_summary(started_at: datetime, results: dict) -> str:
    lines = [f"Date: {started_at:%Y-%m-%d %H:%M}", "", "Sources:"]
    for source in SOURCES:
        rows = results.get(source)
        lines.append(f"  - {source}: skipped (not configured)" if rows is None else f"  - {source}: {rows} rows")
    return "\n".join(lines)


def main() -> int:
    started_at = datetime.now()
    failures = []
    results: dict[str, int | None] = {}

    for source in SOURCES:
        logger.info("=== Running %s ETL ===", source)
        try:
            module = __import__(SOURCE_MODULES[source], fromlist=["run"])
            results[source] = module.run()
        except Exception:  # noqa: BLE001 - keep going, report at the end
            tb = traceback.format_exc()
            logger.exception("%s ETL failed", source)
            send_failure_alert(source, tb)
            failures.append(source)

    if failures:
        logger.error("ETL run completed with failures: %s", failures)
        return 1

    logger.info("ETL run completed successfully for all sources")
    send_success_alert(build_success_summary(started_at, results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
