"""Source 1: Postgres `bookings` table -> analytics.fact_bookings.

The exact column names of the source `bookings` table are not known ahead of
time, so this script introspects information_schema.columns and resolves each
analytics concept (booking id, client, service, price, ...) against a list of
likely candidate column names. Run with `--inspect` to print what it detected
without writing anything, and adjust CANDIDATES below if a column isn't found.

Load strategy: full reload each run, upserted on booking_id (ON CONFLICT DO
UPDATE). Fine for a single-salon bookings table; if it grows large, add an
incremental WHERE clause on an updated_at/modified_at column once one exists.
"""
from __future__ import annotations

import sys
from decimal import Decimal

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

import config
from etl.common import (
    coerce_date,
    date_key,
    parse_money,
    setup_logging,
    track_run,
    upsert_client,
    upsert_payment_method,
    upsert_service,
    upsert_staff,
)

logger = setup_logging("bookings")

# concept -> ordered list of likely source column names, first match wins
CANDIDATES: dict[str, list[str]] = {
    "booking_id": ["id", "booking_id", "uuid"],
    "client_id": ["client_id", "customer_id", "user_id"],
    "client_name": ["client_name", "customer_name", "full_name", "name"],
    "email": ["email", "client_email", "customer_email"],
    "phone": ["phone", "client_phone", "telephone", "phone_number"],
    "service": ["service", "service_name", "treatment", "treatment_name"],
    "staff": ["staff", "staff_name", "employee", "provider", "practitioner"],
    "status": ["status", "booking_status"],
    "duration_minutes": ["duration_minutes", "duration", "duration_min"],
    "price": ["price", "amount", "total", "price_amount", "total_amount"],
    "payment_method": ["payment_method", "payment_type"],
    "booking_date": ["booking_date", "appointment_date", "scheduled_at", "start_time", "date"],
    "created_at": ["created_at", "inserted_at", "booking_date", "date"],
}
REQUIRED_CONCEPTS = ["booking_id", "booking_date"]


def resolve_columns(cur: psycopg.Cursor) -> dict[str, str | None]:
    cur.execute(
        """SELECT column_name FROM information_schema.columns
           WHERE table_schema = %s AND table_name = %s""",
        (config.BOOKINGS_SCHEMA, config.BOOKINGS_TABLE),
    )
    rows = cur.fetchall()
    available = {(row["column_name"] if isinstance(row, dict) else row[0]) for row in rows}
    if not available:
        raise RuntimeError(
            f"Table {config.BOOKINGS_SCHEMA}.{config.BOOKINGS_TABLE} not found or has no columns "
            "- check PGDATABASE/BOOKINGS_SCHEMA/BOOKINGS_TABLE in .env"
        )

    resolved: dict[str, str | None] = {}
    for concept, candidates in CANDIDATES.items():
        resolved[concept] = next((c for c in candidates if c in available), None)

    for concept in REQUIRED_CONCEPTS:
        if resolved[concept] is None:
            raise RuntimeError(
                f"Could not find a source column for required concept '{concept}' among {sorted(available)}. "
                f"Add the real column name to CANDIDATES['{concept}'] in etl/bookings_etl.py."
            )

    missing = [c for c, v in resolved.items() if v is None]
    if missing:
        logger.warning("No source column found for optional concept(s): %s (will be loaded as NULL)", missing)
    logger.info("Resolved bookings columns: %s", {k: v for k, v in resolved.items() if v})
    return resolved


def fetch_bookings(cur: psycopg.Cursor, columns: dict[str, str | None]) -> list[dict]:
    select_items = []
    for concept, col in columns.items():
        if col is not None:
            select_items.append(sql.SQL("{col} AS {alias}").format(col=sql.Identifier(col), alias=sql.Identifier(concept)))
    query = sql.SQL("SELECT {items} FROM {schema}.{table}").format(
        items=sql.SQL(", ").join(select_items),
        schema=sql.Identifier(config.BOOKINGS_SCHEMA),
        table=sql.Identifier(config.BOOKINGS_TABLE),
    )
    cur.execute(query)
    return cur.fetchall()


def load_bookings(conn: psycopg.Connection, rows: list[dict]) -> int:
    loaded = 0
    with conn.cursor() as cur:
        for row in rows:
            booking_id = str(row["booking_id"])
            # booking_date isn't always a native Postgres date type (the real
            # bookings table stores it as plain text, e.g. "2026-08-28"), so
            # normalize whatever comes back before date_key() (which needs a
            # date/datetime) touches it.
            booking_dt = coerce_date(row.get("booking_date"))
            dk = date_key(booking_dt)
            if dk is None:
                logger.warning(
                    "Skipping booking %s: no usable booking_date (got %r)", booking_id, row.get("booking_date")
                )
                continue

            client_key = upsert_client(
                conn,
                source_system="bookings",
                client_id=str(row["client_id"]) if row.get("client_id") is not None else booking_id,
                full_name=row.get("client_name"),
                email=row.get("email"),
                phone=row.get("phone"),
                first_seen_date=booking_dt,
            )
            service_key = upsert_service(conn, row.get("service"))
            staff_key = upsert_staff(conn, row.get("staff"))
            payment_method_key = upsert_payment_method(conn, row.get("payment_method"))

            price = parse_money(row.get("price"))
            duration = row.get("duration_minutes")

            cur.execute(
                """INSERT INTO analytics.fact_bookings
                       (booking_id, date_key, client_key, service_key, staff_key,
                        payment_method_key, status, duration_minutes, price_amount, booking_created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (booking_id) DO UPDATE SET
                       date_key = EXCLUDED.date_key,
                       client_key = EXCLUDED.client_key,
                       service_key = EXCLUDED.service_key,
                       staff_key = EXCLUDED.staff_key,
                       payment_method_key = EXCLUDED.payment_method_key,
                       status = EXCLUDED.status,
                       duration_minutes = EXCLUDED.duration_minutes,
                       price_amount = EXCLUDED.price_amount,
                       booking_created_at = EXCLUDED.booking_created_at""",
                (
                    booking_id, dk, client_key, service_key, staff_key,
                    payment_method_key, row.get("status"),
                    Decimal(str(duration)) if duration is not None else None,
                    price, row.get("created_at"),
                ),
            )
            loaded += 1
    return loaded


def run() -> None:
    import db
    db.apply_schema()

    with db.get_conn() as conn:
        with track_run(conn, "bookings", logger) as state:
            with conn.cursor(row_factory=dict_row) as cur:
                columns = resolve_columns(cur)
                rows = fetch_bookings(cur, columns)
            logger.info("Fetched %s source bookings", len(rows))
            state["rows_loaded"] = load_bookings(conn, rows)
        conn.commit()


if __name__ == "__main__":
    if "--inspect" in sys.argv:
        import db

        with db.get_conn() as conn, conn.cursor() as cur:
            resolve_columns(cur)
        sys.exit(0)
    run()
