-- L'Orchidee analytics warehouse schema
-- Star schema: one schema, shared conformed dimensions, one fact table per source grain.
-- Lives in the SAME Postgres database as the source `bookings` table (lorchidee_bookings),
-- in its own `analytics` schema, so ETL scripts can read source + write destination
-- over a single connection without cross-database plumbing (dblink/fdw).

CREATE SCHEMA IF NOT EXISTS analytics;

-- ---------------------------------------------------------------------------
-- DIMENSIONS
-- ---------------------------------------------------------------------------

-- Pre-populated calendar dimension. date_key = YYYYMMDD as int, joins fast and
-- reads directly as a Looker Studio date field.
CREATE TABLE IF NOT EXISTS analytics.dim_date (
    date_key        INT PRIMARY KEY,
    date            DATE NOT NULL UNIQUE,
    year            SMALLINT NOT NULL,
    quarter         SMALLINT NOT NULL,
    month           SMALLINT NOT NULL,
    month_name      TEXT NOT NULL,
    day             SMALLINT NOT NULL,
    day_of_week     SMALLINT NOT NULL,   -- 1=Monday .. 7=Sunday (ISO)
    day_name        TEXT NOT NULL,
    is_weekend      BOOLEAN NOT NULL,
    iso_week        SMALLINT NOT NULL
);

-- Conformed across bookings + manual sheet transactions.
CREATE TABLE IF NOT EXISTS analytics.dim_client (
    client_key      SERIAL PRIMARY KEY,
    source_system   TEXT NOT NULL,       -- 'bookings' | 'sheet'
    client_id       TEXT NOT NULL,       -- natural id from the source system
    full_name       TEXT,
    email           TEXT,
    phone           TEXT,
    first_seen_date DATE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_system, client_id)
);

CREATE TABLE IF NOT EXISTS analytics.dim_service (
    service_key     SERIAL PRIMARY KEY,
    service_name    TEXT NOT NULL,       -- normalized (trimmed, lowercased at load time)
    category        TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (service_name)
);

CREATE TABLE IF NOT EXISTS analytics.dim_staff (
    staff_key       SERIAL PRIMARY KEY,
    staff_id        TEXT,                -- natural id if the source has one, else NULL
    staff_name      TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (staff_name)
);

CREATE TABLE IF NOT EXISTS analytics.dim_payment_method (
    payment_method_key SERIAL PRIMARY KEY,
    payment_method_name TEXT NOT NULL UNIQUE
);

-- GA4 acquisition channel grain.
CREATE TABLE IF NOT EXISTS analytics.dim_channel (
    channel_key         SERIAL PRIMARY KEY,
    default_channel_group TEXT NOT NULL,
    source              TEXT NOT NULL,
    medium              TEXT NOT NULL,
    campaign            TEXT NOT NULL DEFAULT '(not set)',
    UNIQUE (default_channel_group, source, medium, campaign)
);

-- GA4 page grain.
CREATE TABLE IF NOT EXISTS analytics.dim_page (
    page_key        SERIAL PRIMARY KEY,
    page_path       TEXT NOT NULL,
    page_title      TEXT,
    UNIQUE (page_path)
);

-- Google Business Profile location (future-proofs multi-location).
CREATE TABLE IF NOT EXISTS analytics.dim_location (
    location_key    SERIAL PRIMARY KEY,
    gbp_account_id  TEXT NOT NULL,
    gbp_location_id TEXT NOT NULL,
    location_name   TEXT,
    UNIQUE (gbp_account_id, gbp_location_id)
);

-- ---------------------------------------------------------------------------
-- FACTS
-- ---------------------------------------------------------------------------

-- Grain: one row per booking (source: Postgres `bookings` table).
CREATE TABLE IF NOT EXISTS analytics.fact_bookings (
    booking_key         BIGSERIAL PRIMARY KEY,
    booking_id          TEXT NOT NULL UNIQUE,    -- natural key from source.bookings
    date_key             INT NOT NULL REFERENCES analytics.dim_date(date_key),
    client_key            INT REFERENCES analytics.dim_client(client_key),
    service_key           INT REFERENCES analytics.dim_service(service_key),
    staff_key              INT REFERENCES analytics.dim_staff(staff_key),
    payment_method_key     INT REFERENCES analytics.dim_payment_method(payment_method_key),
    status              TEXT,
    duration_minutes    NUMERIC(6,1),
    price_amount        NUMERIC(10,2),
    currency            CHAR(3) NOT NULL DEFAULT 'CAD',
    booking_created_at  TIMESTAMPTZ,
    _loaded_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_fact_bookings_date ON analytics.fact_bookings(date_key);
CREATE INDEX IF NOT EXISTS ix_fact_bookings_client ON analytics.fact_bookings(client_key);

-- Grain: one row per line in the manual Google Sheet transaction log.
CREATE TABLE IF NOT EXISTS analytics.fact_manual_transactions (
    transaction_key     BIGSERIAL PRIMARY KEY,
    sheet_row_number    INT NOT NULL UNIQUE,     -- 1-based row number in the sheet, used as natural/idempotency key
    date_key             INT NOT NULL REFERENCES analytics.dim_date(date_key),
    client_key            INT REFERENCES analytics.dim_client(client_key),
    service_key           INT REFERENCES analytics.dim_service(service_key),
    payment_method_key     INT REFERENCES analytics.dim_payment_method(payment_method_key),
    duration_minutes    NUMERIC(6,1),
    price_amount         NUMERIC(10,2),
    product_sold_amount NUMERIC(10,2),
    product_name         TEXT,
    note                 TEXT,
    currency              CHAR(3) NOT NULL DEFAULT 'CAD',
    _loaded_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_fact_manual_txn_date ON analytics.fact_manual_transactions(date_key);
CREATE INDEX IF NOT EXISTS ix_fact_manual_txn_client ON analytics.fact_manual_transactions(client_key);

-- Grain: one row per (date, channel) from the GA4 Data API.
CREATE TABLE IF NOT EXISTS analytics.fact_ga4_traffic_daily (
    traffic_key         BIGSERIAL PRIMARY KEY,
    date_key             INT NOT NULL REFERENCES analytics.dim_date(date_key),
    channel_key           INT NOT NULL REFERENCES analytics.dim_channel(channel_key),
    sessions             BIGINT,
    total_users           BIGINT,
    new_users             BIGINT,
    engaged_sessions      BIGINT,
    engagement_rate       NUMERIC(6,4),
    conversions           BIGINT,
    total_revenue         NUMERIC(12,2),
    _loaded_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (date_key, channel_key)
);

-- Grain: one row per (date, page) from the GA4 Data API.
CREATE TABLE IF NOT EXISTS analytics.fact_ga4_page_views_daily (
    page_view_key        BIGSERIAL PRIMARY KEY,
    date_key              INT NOT NULL REFERENCES analytics.dim_date(date_key),
    page_key                INT NOT NULL REFERENCES analytics.dim_page(page_key),
    screen_page_views       BIGINT,
    total_users              BIGINT,
    avg_engagement_time_sec NUMERIC(10,2),
    _loaded_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (date_key, page_key)
);

-- Grain: one row per (date, location, metric) from the Business Profile Performance API.
-- EAV-shaped on purpose: GBP's metric catalogue is long (impressions/searches/calls/
-- direction requests/website clicks/...) and grows over time; this avoids a schema
-- migration every time Google adds a metric. Pivot in the Looker Studio data source
-- or with a view (see analytics.v_gbp_performance_wide below).
CREATE TABLE IF NOT EXISTS analytics.fact_gbp_performance_daily (
    gbp_perf_key    BIGSERIAL PRIMARY KEY,
    date_key         INT NOT NULL REFERENCES analytics.dim_date(date_key),
    location_key       INT NOT NULL REFERENCES analytics.dim_location(location_key),
    metric_name         TEXT NOT NULL,     -- e.g. BUSINESS_IMPRESSIONS_DESKTOP_MAPS, CALL_CLICKS, WEBSITE_CLICKS
    metric_value          BIGINT NOT NULL,
    _loaded_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (date_key, location_key, metric_name)
);

-- Grain: one row per Google review.
CREATE TABLE IF NOT EXISTS analytics.fact_gbp_reviews (
    review_key           BIGSERIAL PRIMARY KEY,
    google_review_id      TEXT NOT NULL UNIQUE,
    location_key            INT NOT NULL REFERENCES analytics.dim_location(location_key),
    date_key                 INT NOT NULL REFERENCES analytics.dim_date(date_key),  -- derived from create_time
    rating                     SMALLINT,
    reviewer_display_name     TEXT,
    comment                    TEXT,
    review_reply_comment      TEXT,
    create_time                TIMESTAMPTZ,
    update_time                TIMESTAMPTZ,
    _loaded_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_fact_gbp_reviews_date ON analytics.fact_gbp_reviews(date_key);

-- Convenience pivot for Looker Studio: wide GBP metrics, one row per date+location.
CREATE OR REPLACE VIEW analytics.v_gbp_performance_wide AS
SELECT
    date_key,
    location_key,
    MAX(metric_value) FILTER (WHERE metric_name = 'BUSINESS_IMPRESSIONS_DESKTOP_MAPS') AS impressions_desktop_maps,
    MAX(metric_value) FILTER (WHERE metric_name = 'BUSINESS_IMPRESSIONS_DESKTOP_SEARCH') AS impressions_desktop_search,
    MAX(metric_value) FILTER (WHERE metric_name = 'BUSINESS_IMPRESSIONS_MOBILE_MAPS') AS impressions_mobile_maps,
    MAX(metric_value) FILTER (WHERE metric_name = 'BUSINESS_IMPRESSIONS_MOBILE_SEARCH') AS impressions_mobile_search,
    MAX(metric_value) FILTER (WHERE metric_name = 'CALL_CLICKS') AS call_clicks,
    MAX(metric_value) FILTER (WHERE metric_name = 'BUSINESS_DIRECTION_REQUESTS') AS direction_requests,
    MAX(metric_value) FILTER (WHERE metric_name = 'WEBSITE_CLICKS') AS website_clicks,
    MAX(metric_value) FILTER (WHERE metric_name = 'BUSINESS_BOOKINGS') AS gbp_bookings,
    MAX(metric_value) FILTER (WHERE metric_name = 'BUSINESS_CONVERSATIONS') AS conversations
FROM analytics.fact_gbp_performance_daily
GROUP BY date_key, location_key;

-- ---------------------------------------------------------------------------
-- OPERATIONAL: ETL run audit log
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS analytics.etl_run_log (
    run_id          BIGSERIAL PRIMARY KEY,
    source          TEXT NOT NULL,       -- 'bookings' | 'ga4' | 'gbp' | 'sheets'
    started_at      TIMESTAMPTZ NOT NULL,
    finished_at     TIMESTAMPTZ,
    status          TEXT NOT NULL,       -- 'running' | 'success' | 'failed'
    rows_loaded     INT,
    error_message   TEXT
);
CREATE INDEX IF NOT EXISTS ix_etl_run_log_source_started ON analytics.etl_run_log(source, started_at DESC);

-- ---------------------------------------------------------------------------
-- Populate the date dimension for a wide, fixed range. Re-run is a no-op
-- thanks to ON CONFLICT DO NOTHING.
-- ---------------------------------------------------------------------------
INSERT INTO analytics.dim_date (date_key, date, year, quarter, month, month_name, day, day_of_week, day_name, is_weekend, iso_week)
SELECT
    TO_CHAR(d, 'YYYYMMDD')::INT,
    d,
    EXTRACT(YEAR FROM d)::SMALLINT,
    EXTRACT(QUARTER FROM d)::SMALLINT,
    EXTRACT(MONTH FROM d)::SMALLINT,
    TO_CHAR(d, 'FMMonth'),
    EXTRACT(DAY FROM d)::SMALLINT,
    EXTRACT(ISODOW FROM d)::SMALLINT,
    TO_CHAR(d, 'FMDay'),
    EXTRACT(ISODOW FROM d) IN (6, 7),
    EXTRACT(WEEK FROM d)::SMALLINT
FROM generate_series('2020-01-01'::DATE, '2031-12-31'::DATE, INTERVAL '1 day') AS d
ON CONFLICT (date_key) DO NOTHING;
