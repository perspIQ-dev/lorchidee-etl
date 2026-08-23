#!/usr/bin/env python
"""One-off bootstrap: load the real Google Sheet transaction log into
analytics.fact_manual_transactions.

Why this exists: bookings/GA4/GBP aren't connected yet, so the Sheet is the
only source with real production data right now. This script gets that data
into Postgres today, using a plain API key (the Sheet is public) instead of
the service account the scheduled etl/sheets_etl.py uses - no service account
setup or sharing step needed to run this.

Usage (run on a host that can reach the Postgres server, e.g. the VPS itself):
    python scripts/sheets_to_analytics.py            # fetch, clean, upsert
    python scripts/sheets_to_analytics.py --dry-run   # fetch + clean + print a
                                                       # summary, then roll back
                                                       # instead of committing

Reuses the same cleaning/upsert logic as etl/sheets_etl.py (column mapping,
money/date parsing, dimension upserts) so a row lands identically whichever
script writes it, and re-running is safe: every row upserts on its sheet row
number, so duplicates can't pile up.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow `import config` / `import db` / `import etl.*` when run directly as
# `python scripts/sheets_to_analytics.py` (scripts/ itself isn't on sys.path).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from googleapiclient.discovery import build

import config
import db
from etl.common import setup_logging, track_run
from etl.sheets_etl import load_rows

logger = setup_logging("sheets_to_analytics")


def fetch_rows_via_api_key() -> list[list[str]]:
    if not config.GOOGLE_API_KEY:
        raise RuntimeError("GOOGLE_API_KEY not set in .env - see .env.example")
    if not config.GOOGLE_SHEET_ID:
        raise RuntimeError("GOOGLE_SHEET_ID not set in .env - see .env.example")

    client = build("sheets", "v4", developerKey=config.GOOGLE_API_KEY)
    resp = (
        client.spreadsheets()
        .values()
        .get(spreadsheetId=config.GOOGLE_SHEET_ID, range=config.GOOGLE_SHEET_RANGE)
        .execute()
    )
    return resp.get("values", [])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Fetch, clean, and print a summary, but roll back instead of committing to Postgres.",
    )
    args = parser.parse_args()

    logger.info("Applying analytics schema (idempotent, safe if it already exists)...")
    db.apply_schema()

    logger.info("Fetching sheet %s range %s via API key...", config.GOOGLE_SHEET_ID, config.GOOGLE_SHEET_RANGE)
    raw_rows = fetch_rows_via_api_key()
    logger.info("Fetched %s raw rows from the sheet", len(raw_rows))

    if args.dry_run:
        # Deliberately skip track_run here: it commits analytics.etl_run_log
        # (and, since that's the same transaction, everything else) as part of
        # marking the run successful - which would commit the data before we
        # ever got to roll it back. Dry-run just loads in an open transaction
        # and always rolls back, with no run recorded.
        with db.get_conn() as conn:
            rows_loaded = load_rows(conn, raw_rows)
            conn.rollback()
        logger.info("--dry-run: rolled back, nothing was written to Postgres")
    else:
        with db.get_conn() as conn:
            with track_run(conn, "sheets_to_analytics", logger) as state:
                state["rows_loaded"] = load_rows(conn, raw_rows)
            # track_run already commits on success; this is a harmless no-op
            # in that case and ensures a commit if that ever changes.
            conn.commit()
        rows_loaded = state["rows_loaded"]
        logger.info("Committed %s rows to analytics.fact_manual_transactions", rows_loaded)

    print_summary(raw_rows, rows_loaded, dry_run=args.dry_run)
    return 0


def print_summary(raw_rows: list[list[str]], loaded: int, dry_run: bool) -> None:
    skipped = len(raw_rows) - loaded
    print()
    print("=" * 60)
    print(f"Sheet rows fetched:  {len(raw_rows)}")
    print(f"Rows loaded:         {loaded}")
    print(f"Rows skipped:        {skipped} (see warnings above, usually unparseable dates)")
    print(f"Mode:                {'DRY RUN (nothing committed)' if dry_run else 'COMMITTED'}")
    print("=" * 60)
    if not dry_run and loaded:
        print(
            "Check it in Postgres:\n"
            "  SELECT COUNT(*) FROM analytics.fact_manual_transactions;\n"
            "  SELECT * FROM analytics.fact_manual_transactions ORDER BY sheet_row_number DESC LIMIT 10;\n"
            "  SELECT * FROM analytics.etl_run_log ORDER BY started_at DESC LIMIT 5;"
        )


if __name__ == "__main__":
    sys.exit(main())
