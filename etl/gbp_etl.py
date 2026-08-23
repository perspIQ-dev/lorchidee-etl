"""Source 3: Google Business Profile -> analytics.fact_gbp_performance_daily + fact_gbp_reviews.

Uses three separate Google APIs (this is normal for GBP, Google split the old
"My Business API" into several services):
  - mybusinessaccountmanagement v1  : list accounts the service account can access
  - mybusinessbusinessinformation v1: list locations under an account
  - businessprofileperformance v1   : daily performance metrics (impressions, calls, clicks...)
  - mybusiness v4 (legacy)          : reviews (rating/comment) - Google has not shipped a
    full v1 replacement for review *content* yet. If your service account/project doesn't
    have this legacy API enabled/allowlisted, the reviews step logs a warning and skips
    rather than failing the whole run.

The service account must be added as a Manager/Owner on the Business Profile
(Business Profile > Users) for any of this to return data.
"""
from __future__ import annotations

from datetime import date, timedelta

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import config
from etl.common import (
    date_key,
    google_credentials,
    service_account_available,
    setup_logging,
    track_run,
    upsert_location,
)

logger = setup_logging("gbp")

SCOPES = ["https://www.googleapis.com/auth/business.manage"]

DAILY_METRICS = [
    "BUSINESS_IMPRESSIONS_DESKTOP_MAPS",
    "BUSINESS_IMPRESSIONS_DESKTOP_SEARCH",
    "BUSINESS_IMPRESSIONS_MOBILE_MAPS",
    "BUSINESS_IMPRESSIONS_MOBILE_SEARCH",
    "CALL_CLICKS",
    "BUSINESS_DIRECTION_REQUESTS",
    "WEBSITE_CLICKS",
    "BUSINESS_BOOKINGS",
    "BUSINESS_CONVERSATIONS",
]


def _creds():
    return google_credentials(SCOPES)


def resolve_account_and_location(creds) -> tuple[str, str, str | None]:
    """Returns (account_id, location_id, location_display_name). Uses
    GBP_ACCOUNT_ID/GBP_LOCATION_ID from config if set, else auto-discovers
    the first account/location the service account can see."""
    account_id, location_id = config.GBP_ACCOUNT_ID, config.GBP_LOCATION_ID
    location_name = None

    if not account_id:
        accounts_client = build("mybusinessaccountmanagement", "v1", credentials=creds)
        accounts = accounts_client.accounts().list().execute().get("accounts", [])
        if not accounts:
            raise RuntimeError("Service account has no accessible Business Profile accounts")
        account_id = accounts[0]["name"].split("/")[-1]
        logger.info("Auto-discovered GBP account: %s", account_id)

    if not location_id:
        info_client = build("mybusinessbusinessinformation", "v1", credentials=creds)
        locations = (
            info_client.accounts()
            .locations()
            .list(parent=f"accounts/{account_id}", readMask="name,title")
            .execute()
            .get("locations", [])
        )
        if not locations:
            raise RuntimeError(f"No locations found under account {account_id}")
        location_id = locations[0]["name"].split("/")[-1]
        location_name = locations[0].get("title")
        logger.info("Auto-discovered GBP location: %s (%s)", location_id, location_name)

    return account_id, location_id, location_name


def fetch_performance(creds, location_id: str) -> list[dict]:
    client = build("businessprofileperformance", "v1", credentials=creds)
    end = date.today() - timedelta(days=1)  # GBP metrics typically lag ~1-2 days
    start = end - timedelta(days=config.LOOKBACK_DAYS)

    request = client.locations().fetchMultiDailyMetricsTimeSeries(
        location=f"locations/{location_id}",
        dailyMetrics=DAILY_METRICS,
        **{
            "dailyRange.startDate.year": start.year, "dailyRange.startDate.month": start.month, "dailyRange.startDate.day": start.day,
            "dailyRange.endDate.year": end.year, "dailyRange.endDate.month": end.month, "dailyRange.endDate.day": end.day,
        },
    )
    resp = request.execute()

    out = []
    for series in resp.get("multiDailyMetricTimeSeries", []):
        for dm in series.get("dailyMetricTimeSeries", []):
            metric_name = dm.get("dailyMetric")
            for dv in dm.get("timeSeries", {}).get("datedValues", []):
                d = dv.get("date", {})
                if "year" not in d or "value" not in dv:
                    continue
                out.append({
                    "date": date(d["year"], d["month"], d["day"]),
                    "metric_name": metric_name,
                    "value": int(dv["value"]),
                })
    return out


def load_performance(conn, location_key: int, rows: list[dict]) -> int:
    loaded = 0
    with conn.cursor() as cur:
        for r in rows:
            cur.execute(
                """INSERT INTO analytics.fact_gbp_performance_daily
                       (date_key, location_key, metric_name, metric_value)
                   VALUES (%s,%s,%s,%s)
                   ON CONFLICT (date_key, location_key, metric_name) DO UPDATE SET
                       metric_value = EXCLUDED.metric_value""",
                (date_key(r["date"]), location_key, r["metric_name"], r["value"]),
            )
            loaded += 1
    return loaded


def fetch_reviews(creds, account_id: str, location_id: str) -> list[dict]:
    client = build("mybusiness", "v4", credentials=creds)
    parent = f"accounts/{account_id}/locations/{location_id}"
    reviews, page_token = [], None
    while True:
        resp = client.accounts().locations().reviews().list(parent=parent, pageToken=page_token).execute()
        reviews.extend(resp.get("reviews", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return reviews


_STAR_RATING = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5}


def load_reviews(conn, location_key: int, reviews: list[dict]) -> int:
    loaded = 0
    with conn.cursor() as cur:
        for rv in reviews:
            create_time = rv.get("createTime")
            update_time = rv.get("updateTime")
            reply = rv.get("reviewReply", {}).get("comment")
            cur.execute(
                """INSERT INTO analytics.fact_gbp_reviews
                       (google_review_id, location_key, date_key, rating, reviewer_display_name,
                        comment, review_reply_comment, create_time, update_time)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (google_review_id) DO UPDATE SET
                       rating = EXCLUDED.rating, comment = EXCLUDED.comment,
                       review_reply_comment = EXCLUDED.review_reply_comment,
                       update_time = EXCLUDED.update_time""",
                (
                    rv["reviewId"], location_key,
                    date_key(date.fromisoformat(create_time[:10])) if create_time else None,
                    _STAR_RATING.get(rv.get("starRating")),
                    rv.get("reviewer", {}).get("displayName"),
                    rv.get("comment"),
                    reply,
                    create_time,
                    update_time,
                ),
            )
            loaded += 1
    return loaded


def run() -> None:
    import db

    if not service_account_available():
        logger.warning(
            "Service account file not found at %s - skipping GBP ETL", config.GOOGLE_SERVICE_ACCOUNT_FILE
        )
        return

    creds = _creds()
    account_id, location_id, location_name = resolve_account_and_location(creds)

    with db.get_conn() as conn:
        with track_run(conn, "gbp", logger) as state:
            location_key = upsert_location(conn, account_id, location_id, location_name)

            perf_rows = fetch_performance(creds, location_id)
            n1 = load_performance(conn, location_key, perf_rows)

            n2 = 0
            try:
                reviews = fetch_reviews(creds, account_id, location_id)
                n2 = load_reviews(conn, location_key, reviews)
            except HttpError as exc:
                logger.warning("Reviews fetch failed (legacy mybusiness v4 API may not be enabled): %s", exc)

            state["rows_loaded"] = n1 + n2
        conn.commit()


if __name__ == "__main__":
    run()
