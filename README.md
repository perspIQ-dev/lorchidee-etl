# L'Orchidee ETL

Consolidates four data sources into one clean star-schema `analytics` schema
in the existing Postgres database on the VPS (`lorchidee_bookings` @
149.56.103.121), ready to plug into Looker Studio.

## Sources -> destination

| # | Source | Script | Destination facts |
|---|--------|--------|--------------------|
| 1 | Postgres `bookings` table (same VPS) | `etl/bookings_etl.py` | `analytics.fact_bookings` |
| 2 | GA4 Data API | `etl/ga4_etl.py` | `analytics.fact_ga4_traffic_daily`, `analytics.fact_ga4_page_views_daily` |
| 3 | Google Business Profile API | `etl/gbp_etl.py` | `analytics.fact_gbp_performance_daily`, `analytics.fact_gbp_reviews` |
| 4 | Google Sheet (manual transaction log) | `etl/sheets_etl.py` | `analytics.fact_manual_transactions` |

Shared dimensions (`dim_date`, `dim_client`, `dim_service`, `dim_staff`,
`dim_payment_method`, `dim_channel`, `dim_page`, `dim_location`) are
conformed across sources so, e.g., a client shows up consistently whether
they came from `bookings` or the manual sheet. See `sql/001_schema_analytics.sql`
for the full DDL and design notes (why GBP metrics are EAV-shaped, etc).

Every run is recorded in `analytics.etl_run_log` (source, start/end time,
status, rows loaded, error message) for auditing/alerting.

## Setup

```bash
cd lorchidee-etl
python -m venv .venv
source .venv/bin/activate        # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
cp .env.example .env              # then fill in real values
```

Fill in `.env`:
- `PGPASSWORD` and any of `PGHOST/PGPORT/PGDATABASE/PGUSER` that differ from the defaults.
- `BOOKINGS_SCHEMA`/`BOOKINGS_TABLE` if `bookings` isn't in `public`.
- `GOOGLE_SERVICE_ACCOUNT_FILE` - see below.
- `GA4_PROPERTY_ID`, e.g. `properties/123456789`.
- `GBP_ACCOUNT_ID`/`GBP_LOCATION_ID` - optional, auto-discovered if blank.
- `GOOGLE_SHEET_ID` is already set to the salon's sheet; adjust `GOOGLE_SHEET_RANGE` if columns change.

### Google service account

1. In Google Cloud Console, create a service account and download its JSON key to `secrets/service_account.json`.
2. Enable these APIs on the project: Google Analytics Data API, My Business Account Management API, My Business Business Information API, Business Profile Performance API, Google Sheets API. (The legacy My Business API v4, used only for review content, may need a separate access request from Google - if it's not available the reviews step just logs a warning and skips.)
3. Share access with the service account's `client_email`:
   - **GA4**: Admin > Property Access Management > add the service account as Viewer.
   - **Business Profile**: business.google.com > Users > add the service account's email as a Manager.
   - **Sheet**: click Share on the sheet, add the service account's email as Viewer.

### Bookings column mapping

The source `bookings` table's exact columns weren't known ahead of time, so
`etl/bookings_etl.py` introspects `information_schema.columns` and matches
each concept (booking id, client, service, price, ...) against a list of
likely candidate names (see `CANDIDATES` at the top of the file). Run:

```bash
python -m etl.bookings_etl --inspect
```

to see what it detected without writing anything. If a column isn't found,
add its real name to the matching `CANDIDATES[...]` list.

## Running

```bash
python db.py            # one-time: create the analytics schema (also runs automatically before each ETL)
python run_all.py        # run all four sources
python -m etl.bookings_etl   # or run one source at a time
python -m etl.ga4_etl
python -m etl.gbp_etl
python -m etl.sheets_etl
```

## Daily cron (on the VPS)

1. Deploy this folder to the VPS, e.g. `/opt/lorchidee-etl`, with `.venv` set up and `.env`/`secrets/service_account.json` in place (never commit these - see `.gitignore`).
2. `chmod +x cron/run_etl.sh`
3. Install the schedule: `crontab -e`, paste the line from `cron/crontab.example` (adjusted to your deploy path). Default is daily at 05:00 server time.
4. Logs land in `logs/cron_YYYY-MM-DD.log` plus a per-source `logs/<source>.log`, and every run is queryable via `SELECT * FROM analytics.etl_run_log ORDER BY started_at DESC;`.

## Looker Studio

Connect Looker Studio's Postgres connector directly to the VPS database,
schema `analytics`. Join fact tables to `dim_date` on `date_key` for a date
filter control, and to the other dimensions on their `*_key` columns. Use
`analytics.v_gbp_performance_wide` if you want GBP metrics pivoted into
columns instead of the raw EAV `fact_gbp_performance_daily`.
