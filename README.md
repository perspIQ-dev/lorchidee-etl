# L'Orchidee ETL

A small analytics ETL pipeline for a beauty salon. It consolidates four
disconnected data sources into one clean star-schema `analytics` schema in
the salon's existing Postgres database, ready to plug straight into Looker
Studio for reporting.

**Stack:** Python, [psycopg 3](https://www.psycopg.org/psycopg3/), [google-api-python-client](https://github.com/googleapis/google-api-python-client).

## Sources -> destination

| # | Source | Script | Destination facts |
|---|--------|--------|--------------------|
| 1 | Postgres `bookings` table (same server) | `etl/bookings_etl.py` | `analytics.fact_bookings` |
| 2 | GA4 Data API | `etl/ga4_etl.py` | `analytics.fact_ga4_traffic_daily`, `analytics.fact_ga4_page_views_daily` |
| 3 | Google Business Profile API | `etl/gbp_etl.py` | `analytics.fact_gbp_performance_daily`, `analytics.fact_gbp_reviews` |
| 4 | Google Sheet (manual transaction log) | `scripts/sheets_to_analytics.py` | `analytics.fact_manual_transactions` |

`run_all.py` runs the Sheet through `scripts/sheets_to_analytics.py`, not
`etl/sheets_etl.py` - it authenticates with a plain API key against the
public Sheet, which is what's actually set up and working today. GA4/GBP
authenticate with the service account, which isn't configured yet, so
`run_all.py` skips them (logs a warning, doesn't fail the run) until
`secrets/service_account.json` exists; `etl/sheets_etl.py` also uses that
service account and is the one to switch back to once it's in place, since
it covers GA4/GBP too instead of needing a separate API key.

## Design

Star schema, one `analytics` schema alongside the source `bookings` table in
the same Postgres database — no cross-database plumbing needed. See
`sql/001_schema_analytics.sql` for the full DDL.

**Shared/conformed dimensions:** `dim_date`, `dim_client`, `dim_service`,
`dim_staff`, `dim_payment_method`, `dim_channel`, `dim_page`, `dim_location`.
A client shows up consistently in reporting whether they came from
`bookings` or the manual sheet, because both write into the same
`dim_client` keyed on `(source_system, client_id)`.

**Facts** are grain-per-source: one row per booking, one row per manual
transaction, one row per (date, GA4 channel), one row per (date, GA4 page),
one row per (date, location, GBP metric), one row per Google review.
`fact_gbp_performance_daily` is EAV-shaped (`metric_name`/`metric_value`) on
purpose — GBP's metric catalogue is long and grows over time, and this
avoids a schema migration every time Google adds one. There's a pivoted
convenience view, `analytics.v_gbp_performance_wide`, for anything that
wants columns instead.

**Idempotency:** every fact table upserts on a natural/business key
(`booking_id`, `sheet_row_number`, `(date_key, channel_key)`,
`google_review_id`, ...), so reruns and daily overlap windows never create
duplicates.

**Audit trail:** every ETL run — success or failure — writes a row to
`analytics.etl_run_log` (source, start/end time, status, rows loaded, error
message), queryable directly for monitoring.

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
- `GOOGLE_SERVICE_ACCOUNT_FILE` — see below.
- `GA4_PROPERTY_ID`, e.g. `properties/123456789`.
- `GBP_ACCOUNT_ID`/`GBP_LOCATION_ID` — optional, auto-discovered if blank.
- `GOOGLE_SHEET_ID` is already set to the salon's sheet.
- `RESEND_API_KEY` — for failure email alerts (see below). Optional: without it, `run_all.py` just logs a warning and keeps going instead of emailing.

### Google service account

Authentication is a single service account (no interactive login), which is
the right shape for an unattended daily cron job.

1. In Google Cloud Console, create a service account and download its JSON key to `secrets/service_account.json`.
2. Enable these APIs on the project: Google Analytics Data API, My Business Account Management API, My Business Business Information API, Business Profile Performance API, Google Sheets API. (The legacy My Business API v4, used only for review content, sometimes needs a separate access request from Google — if it's not available, the reviews step just logs a warning and skips rather than failing the whole run.)
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
python db.py                  # one-time: create the analytics schema (also runs automatically before each ETL)
python run_all.py             # run all four sources
python -m etl.bookings_etl    # or run one source at a time
python -m etl.ga4_etl
python -m etl.gbp_etl
python scripts/sheets_to_analytics.py   # what run_all.py actually calls for the Sheet today
python -m etl.sheets_etl                # service-account version, once GA4/GBP are set up
```

`run_all.py` runs every source, isolates failures per-source (one bad source
doesn't block the others), and exits non-zero if anything failed — so cron's
mail/alerting notices.

### Failure alerting

Whenever a source fails, `run_all.py` emails `yanou.yadi@gmail.com` (from
`lorchidee@send.lorchidee.ca`) via [Resend](https://resend.com)
(`alerting.py`), subject `ETL Alert: lorchidee-etl failed`, body is the full
traceback. Requires `RESEND_API_KEY` in `.env` and the sending domain
(`send.lorchidee.ca`) verified in Resend; if the key is unset, it just logs
a warning and moves on rather than failing the run. A failed source is still
always logged in `analytics.etl_run_log` regardless of whether the email
sends.

## Daily cron

1. Deploy this folder anywhere with the venv set up and `.env` / `secrets/service_account.json` in place (never commit these — see `.gitignore`).
2. `chmod +x cron/run_etl.sh`
3. Install the schedule: `crontab -e`, paste the line from `cron/crontab.example` (adjusted to your deploy path). Runs daily at 02:00 server time.
4. Logs land in `logs/cron_YYYY-MM-DD.log` plus a per-source `logs/<source>.log`, and every run is queryable via `SELECT * FROM analytics.etl_run_log ORDER BY started_at DESC;`.

## Looker Studio

Connect Looker Studio's Postgres connector to the salon's Postgres server,
schema `analytics`. Join fact tables to `dim_date` on `date_key` for a date
filter control, and to the other dimensions on their `*_key` columns. Use
`analytics.v_gbp_performance_wide` if you want GBP metrics pivoted into
columns instead of the raw EAV `fact_gbp_performance_daily`.
