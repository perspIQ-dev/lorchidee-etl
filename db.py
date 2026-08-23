"""Postgres connection + one-time schema bootstrap."""
from pathlib import Path

import psycopg

import config

SCHEMA_SQL_FILE = Path(__file__).resolve().parent / "sql" / "001_schema_analytics.sql"


def get_conn() -> psycopg.Connection:
    """Autocommit off by default: callers control transactions per ETL run."""
    return psycopg.connect(config.PG_CONNINFO)


def apply_schema() -> None:
    """Idempotent: creates the analytics schema/tables/views if they don't exist yet.
    Safe to run before every ETL run (cheap no-op once the schema exists)."""
    sql = SCHEMA_SQL_FILE.read_text(encoding="utf-8")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()


if __name__ == "__main__":
    apply_schema()
    print("analytics schema applied.")
