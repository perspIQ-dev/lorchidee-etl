"""Central config: loads .env once, exposes typed settings to every ETL script."""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _get(name: str, default: str | None = None, required: bool = False) -> str:
    val = os.environ.get(name, default)
    if required and not val:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return val


# Postgres
PGHOST = _get("PGHOST", required=True)
PGPORT = int(_get("PGPORT", "5432"))
PGDATABASE = _get("PGDATABASE", required=True)
PGUSER = _get("PGUSER", required=True)
PGPASSWORD = _get("PGPASSWORD", required=True)

BOOKINGS_SCHEMA = _get("BOOKINGS_SCHEMA", "public")
BOOKINGS_TABLE = _get("BOOKINGS_TABLE", "bookings")
ANALYTICS_SCHEMA = "analytics"

# Google
GOOGLE_SERVICE_ACCOUNT_FILE = _get("GOOGLE_SERVICE_ACCOUNT_FILE", str(BASE_DIR / "secrets" / "service_account.json"))
# Simple read-only auth for the (public) Sheet - used by scripts/sheets_to_analytics.py.
# The scheduled etl/sheets_etl.py still uses the service account, since that
# also covers GA4/GBP and keeps one auth path for the daily pipeline.
GOOGLE_API_KEY = _get("GOOGLE_API_KEY", "")
GA4_PROPERTY_ID = _get("GA4_PROPERTY_ID", "")
GBP_ACCOUNT_ID = _get("GBP_ACCOUNT_ID", "")
GBP_LOCATION_ID = _get("GBP_LOCATION_ID", "")
GOOGLE_SHEET_ID = _get("GOOGLE_SHEET_ID", "")
GOOGLE_SHEET_RANGE = _get("GOOGLE_SHEET_RANGE", "A2:J")

# Misc
LOG_LEVEL = _get("LOG_LEVEL", "INFO")
LOG_DIR = Path(_get("LOG_DIR", str(BASE_DIR / "logs")))
LOOKBACK_DAYS = int(_get("LOOKBACK_DAYS", "3"))

PG_CONNINFO = f"host={PGHOST} port={PGPORT} dbname={PGDATABASE} user={PGUSER} password={PGPASSWORD}"
