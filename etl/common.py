"""Shared helpers used by every ETL script: logging, dimension upserts, run
bookkeeping, money/date parsing, and the Google API credentials builder."""
from __future__ import annotations

import contextlib
import logging
import re
import sys
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql

import config

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(source: str) -> logging.Logger:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(source)
    logger.setLevel(config.LOG_LEVEL)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")

    file_handler = logging.FileHandler(config.LOG_DIR / f"{source}.log", encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(fmt)
    logger.addHandler(stream_handler)

    return logger


# ---------------------------------------------------------------------------
# ETL run bookkeeping (analytics.etl_run_log)
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def track_run(conn: psycopg.Connection, source: str, logger: logging.Logger):
    """Context manager: records a row in analytics.etl_run_log for this run,
    marks it success/failed on exit, and lets the caller report rows_loaded."""
    started_at = datetime.now()
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO analytics.etl_run_log (source, started_at, status)
               VALUES (%s, %s, 'running') RETURNING run_id""",
            (source, started_at),
        )
        run_id = cur.fetchone()[0]
    conn.commit()

    state = {"rows_loaded": 0}
    try:
        yield state
    except Exception as exc:  # noqa: BLE001 - we re-raise after logging the run
        logger.exception("ETL run failed: %s", exc)
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE analytics.etl_run_log
                   SET finished_at = %s, status = 'failed', error_message = %s, rows_loaded = %s
                   WHERE run_id = %s""",
                (datetime.now(), str(exc)[:2000], state["rows_loaded"], run_id),
            )
        conn.commit()
        raise
    else:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE analytics.etl_run_log
                   SET finished_at = %s, status = 'success', rows_loaded = %s
                   WHERE run_id = %s""",
                (datetime.now(), state["rows_loaded"], run_id),
            )
        conn.commit()
        logger.info("ETL run finished: %s rows loaded", state["rows_loaded"])


# ---------------------------------------------------------------------------
# Dimension upserts
# ---------------------------------------------------------------------------

def date_key(d: date | datetime | None) -> int | None:
    if d is None:
        return None
    if isinstance(d, datetime):
        d = d.date()
    return int(d.strftime("%Y%m%d"))


def upsert_dimension(
    conn: psycopg.Connection,
    table: str,
    key_col: str,
    natural_cols: dict[str, Any],
    extra_cols: dict[str, Any] | None = None,
) -> int | None:
    """Generic get-or-create for a dimension row, keyed on natural_cols
    (which must match a UNIQUE constraint on `table`). Returns the surrogate
    key, or None if every natural column value is None/empty (nothing to key on).

    extra_cols are set on INSERT and refreshed on conflict (e.g. a name that
    may be corrected upstream), but are never part of the identity.
    """
    if all(v in (None, "") for v in natural_cols.values()):
        return None

    extra_cols = extra_cols or {}
    all_cols = {**natural_cols, **extra_cols}
    col_idents = [sql.Identifier(c) for c in all_cols]
    conflict_idents = [sql.Identifier(c) for c in natural_cols]

    if extra_cols:
        update_clause = sql.SQL(", ").join(
            sql.SQL("{col} = EXCLUDED.{col}").format(col=sql.Identifier(c)) for c in extra_cols
        )
        on_conflict = sql.SQL("DO UPDATE SET {update}").format(update=update_clause)
    else:
        on_conflict = sql.SQL("DO NOTHING")

    query = sql.SQL(
        "INSERT INTO {table} ({cols}) VALUES ({placeholders}) "
        "ON CONFLICT ({conflict_cols}) {on_conflict} "
        "RETURNING {key_col}"
    ).format(
        table=sql.Identifier("analytics", table),
        cols=sql.SQL(", ").join(col_idents),
        placeholders=sql.SQL(", ").join([sql.Placeholder()] * len(all_cols)),
        conflict_cols=sql.SQL(", ").join(conflict_idents),
        on_conflict=on_conflict,
        key_col=sql.Identifier(key_col),
    )
    with conn.cursor() as cur:
        cur.execute(query, list(all_cols.values()))
        row = cur.fetchone()
        if row is None:
            # DO NOTHING path with no extra_cols hit an existing row without returning it
            select_query = sql.SQL("SELECT {key_col} FROM {table} WHERE {where}").format(
                key_col=sql.Identifier(key_col),
                table=sql.Identifier("analytics", table),
                where=sql.SQL(" AND ").join(
                    sql.SQL("{c} = %s").format(c=sql.Identifier(c)) for c in natural_cols
                ),
            )
            cur.execute(select_query, list(natural_cols.values()))
            row = cur.fetchone()
        return row[0] if row else None


def upsert_client(conn: psycopg.Connection, source_system: str, client_id: str, full_name: str | None = None,
                   email: str | None = None, phone: str | None = None, first_seen_date: date | None = None) -> int | None:
    return upsert_dimension(
        conn,
        table="dim_client",
        key_col="client_key",
        natural_cols={"source_system": source_system, "client_id": client_id},
        extra_cols={"full_name": full_name, "email": email, "phone": phone,
                    "first_seen_date": first_seen_date, "updated_at": datetime.now()},
    )


def upsert_service(conn: psycopg.Connection, service_name: str | None, category: str | None = None) -> int | None:
    if not service_name:
        return None
    return upsert_dimension(
        conn,
        table="dim_service",
        key_col="service_key",
        natural_cols={"service_name": service_name.strip().lower()},
        extra_cols={"category": category} if category else None,
    )


def upsert_staff(conn: psycopg.Connection, staff_name: str | None, staff_id: str | None = None) -> int | None:
    if not staff_name:
        return None
    return upsert_dimension(
        conn,
        table="dim_staff",
        key_col="staff_key",
        natural_cols={"staff_name": staff_name.strip()},
        extra_cols={"staff_id": staff_id} if staff_id else None,
    )


def upsert_payment_method(conn: psycopg.Connection, payment_method_name: str | None) -> int | None:
    if not payment_method_name:
        return None
    return upsert_dimension(
        conn,
        table="dim_payment_method",
        key_col="payment_method_key",
        natural_cols={"payment_method_name": payment_method_name.strip()},
    )


def upsert_channel(conn: psycopg.Connection, default_channel_group: str, source: str, medium: str,
                    campaign: str = "(not set)") -> int | None:
    return upsert_dimension(
        conn,
        table="dim_channel",
        key_col="channel_key",
        natural_cols={
            "default_channel_group": default_channel_group or "(not set)",
            "source": source or "(not set)",
            "medium": medium or "(not set)",
            "campaign": campaign or "(not set)",
        },
    )


def upsert_page(conn: psycopg.Connection, page_path: str, page_title: str | None = None) -> int | None:
    return upsert_dimension(
        conn,
        table="dim_page",
        key_col="page_key",
        natural_cols={"page_path": page_path},
        extra_cols={"page_title": page_title} if page_title else None,
    )


def upsert_location(conn: psycopg.Connection, gbp_account_id: str, gbp_location_id: str,
                     location_name: str | None = None) -> int | None:
    return upsert_dimension(
        conn,
        table="dim_location",
        key_col="location_key",
        natural_cols={"gbp_account_id": gbp_account_id, "gbp_location_id": gbp_location_id},
        extra_cols={"location_name": location_name} if location_name else None,
    )


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

_MONEY_RE = re.compile(r"[^0-9.\-]")


def parse_money(raw: Any) -> Decimal | None:
    """Parses values like 'CA$285.00', '1,234.50', '' -> Decimal or None."""
    if raw is None:
        return None
    if isinstance(raw, (int, float, Decimal)):
        return Decimal(str(raw))
    text = str(raw).strip()
    if not text:
        return None
    cleaned = _MONEY_RE.sub("", text)
    if not cleaned or cleaned in ("-", "."):
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def parse_date_ddmmyyyy(raw: str) -> date | None:
    if not raw:
        return None
    raw = raw.strip()
    for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Google credentials
# ---------------------------------------------------------------------------

def google_credentials(scopes: list[str]):
    from google.oauth2 import service_account

    return service_account.Credentials.from_service_account_file(
        config.GOOGLE_SERVICE_ACCOUNT_FILE, scopes=scopes
    )


def service_account_available() -> bool:
    """GA4/GBP need the service account file; check before using it so
    scripts can skip with a warning instead of crashing when it's not set
    up yet (e.g. only the Sheet, via API key, is configured so far)."""
    return Path(config.GOOGLE_SERVICE_ACCOUNT_FILE).is_file()
