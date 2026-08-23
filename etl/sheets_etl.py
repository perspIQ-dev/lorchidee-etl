"""Source 4: Google Sheet (manual transaction log) -> analytics.fact_manual_transactions.

Confirmed sheet columns (in order), A:J, header in row 1:
  Date | ID_client | nom_client | Service | Duree (min) | Prix | Produit_vendu_$ | Produit | Methode_paiement | note

Dates are DD-MM-YYYY. Prix / Produit_vendu_$ are formatted like "CA$285.00".
sheet_row_number (the actual row number in the sheet) is the idempotency key,
so re-running is safe as long as rows aren't reordered/deleted upstream.
"""
from __future__ import annotations

from googleapiclient.discovery import build

import config
from etl.common import (
    date_key,
    google_credentials,
    parse_date_ddmmyyyy,
    parse_money,
    setup_logging,
    track_run,
    upsert_client,
    upsert_payment_method,
    upsert_service,
)

logger = setup_logging("sheets")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

COL_DATE, COL_CLIENT_ID, COL_CLIENT_NAME, COL_SERVICE, COL_DURATION, COL_PRICE, \
    COL_PRODUCT_AMOUNT, COL_PRODUCT_NAME, COL_PAYMENT_METHOD, COL_NOTE = range(10)


def fetch_rows() -> list[list[str]]:
    creds = google_credentials(SCOPES)
    client = build("sheets", "v4", credentials=creds)
    resp = (
        client.spreadsheets()
        .values()
        .get(spreadsheetId=config.GOOGLE_SHEET_ID, range=config.GOOGLE_SHEET_RANGE)
        .execute()
    )
    return resp.get("values", [])


def load_rows(conn, rows: list[list[str]], header_row: int = 2) -> int:
    loaded = 0
    with conn.cursor() as cur:
        for i, row in enumerate(rows):
            sheet_row_number = header_row + i

            def cell(idx: int) -> str | None:
                return row[idx].strip() if idx < len(row) and row[idx] not in (None, "") else None

            raw_date = cell(COL_DATE)
            txn_date = parse_date_ddmmyyyy(raw_date) if raw_date else None
            if txn_date is None:
                logger.warning("Skipping sheet row %s: unparseable date %r", sheet_row_number, raw_date)
                continue

            client_id = cell(COL_CLIENT_ID)
            client_key = None
            if client_id:
                client_key = upsert_client(
                    conn, source_system="sheet", client_id=client_id,
                    full_name=cell(COL_CLIENT_NAME), first_seen_date=txn_date,
                )

            service_key = upsert_service(conn, cell(COL_SERVICE))
            payment_method_key = upsert_payment_method(conn, cell(COL_PAYMENT_METHOD))

            duration_raw = cell(COL_DURATION)
            duration = float(duration_raw) if duration_raw else None

            cur.execute(
                """INSERT INTO analytics.fact_manual_transactions
                       (sheet_row_number, date_key, client_key, service_key, payment_method_key,
                        duration_minutes, price_amount, product_sold_amount, product_name, note)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (sheet_row_number) DO UPDATE SET
                       date_key = EXCLUDED.date_key, client_key = EXCLUDED.client_key,
                       service_key = EXCLUDED.service_key, payment_method_key = EXCLUDED.payment_method_key,
                       duration_minutes = EXCLUDED.duration_minutes, price_amount = EXCLUDED.price_amount,
                       product_sold_amount = EXCLUDED.product_sold_amount, product_name = EXCLUDED.product_name,
                       note = EXCLUDED.note""",
                (
                    sheet_row_number, date_key(txn_date), client_key, service_key, payment_method_key,
                    duration, parse_money(cell(COL_PRICE)), parse_money(cell(COL_PRODUCT_AMOUNT)),
                    cell(COL_PRODUCT_NAME), cell(COL_NOTE),
                ),
            )
            loaded += 1
    return loaded


def run() -> None:
    import db
    db.apply_schema()

    if not config.GOOGLE_SHEET_ID:
        logger.warning("GOOGLE_SHEET_ID not set - skipping Sheets ETL")
        return

    with db.get_conn() as conn:
        with track_run(conn, "sheets", logger) as state:
            rows = fetch_rows()
            logger.info("Fetched %s sheet rows", len(rows))
            state["rows_loaded"] = load_rows(conn, rows)
        conn.commit()


if __name__ == "__main__":
    run()
