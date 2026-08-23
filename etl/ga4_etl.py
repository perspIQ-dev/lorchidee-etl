"""Source 2: GA4 Data API -> analytics.fact_ga4_traffic_daily + fact_ga4_page_views_daily.

Requires GA4_PROPERTY_ID (format "properties/123456789") and a service account
granted at least Viewer access on that GA4 property.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from googleapiclient.discovery import build

import config
from etl.common import google_credentials, setup_logging, track_run, upsert_channel, upsert_page

logger = setup_logging("ga4")

SCOPES = ["https://www.googleapis.com/auth/analytics.readonly"]


def _client():
    creds = google_credentials(SCOPES)
    return build("analyticsdata", "v1beta", credentials=creds)


def _date_range() -> tuple[str, str]:
    end = date.today()
    start = end - timedelta(days=config.LOOKBACK_DAYS)
    return start.isoformat(), end.isoformat()


def fetch_channel_report(client) -> list[dict]:
    start, end = _date_range()
    body = {
        "dateRanges": [{"startDate": start, "endDate": end}],
        "dimensions": [
            {"name": "date"},
            {"name": "sessionDefaultChannelGroup"},
            {"name": "sessionSource"},
            {"name": "sessionMedium"},
            {"name": "sessionCampaignName"},
        ],
        "metrics": [
            {"name": "sessions"},
            {"name": "totalUsers"},
            {"name": "newUsers"},
            {"name": "engagedSessions"},
            {"name": "engagementRate"},
            {"name": "conversions"},
            {"name": "totalRevenue"},
        ],
        "limit": 100000,
    }
    resp = client.properties().runReport(property=config.GA4_PROPERTY_ID, body=body).execute()
    return _rows_from_response(resp)


def fetch_page_report(client) -> list[dict]:
    start, end = _date_range()
    body = {
        "dateRanges": [{"startDate": start, "endDate": end}],
        "dimensions": [{"name": "date"}, {"name": "pagePath"}, {"name": "pageTitle"}],
        "metrics": [
            {"name": "screenPageViews"},
            {"name": "totalUsers"},
            {"name": "userEngagementDuration"},
        ],
        "limit": 100000,
    }
    resp = client.properties().runReport(property=config.GA4_PROPERTY_ID, body=body).execute()
    return _rows_from_response(resp)


def _rows_from_response(resp: dict) -> list[dict]:
    dim_names = [d["name"] for d in resp.get("dimensionHeaders", [])]
    metric_names = [m["name"] for m in resp.get("metricHeaders", [])]
    out = []
    for row in resp.get("rows", []):
        rec = {}
        for name, val in zip(dim_names, row.get("dimensionValues", [])):
            rec[name] = val.get("value")
        for name, val in zip(metric_names, row.get("metricValues", [])):
            rec[name] = val.get("value")
        out.append(rec)
    return out


def load_channel_rows(conn, rows: list[dict]) -> int:
    loaded = 0
    with conn.cursor() as cur:
        for r in rows:
            dk = int(r["date"])  # GA4 returns YYYYMMDD
            channel_key = upsert_channel(
                conn,
                default_channel_group=r.get("sessionDefaultChannelGroup"),
                source=r.get("sessionSource"),
                medium=r.get("sessionMedium"),
                campaign=r.get("sessionCampaignName"),
            )
            cur.execute(
                """INSERT INTO analytics.fact_ga4_traffic_daily
                       (date_key, channel_key, sessions, total_users, new_users, engaged_sessions,
                        engagement_rate, conversions, total_revenue)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (date_key, channel_key) DO UPDATE SET
                       sessions = EXCLUDED.sessions, total_users = EXCLUDED.total_users,
                       new_users = EXCLUDED.new_users, engaged_sessions = EXCLUDED.engaged_sessions,
                       engagement_rate = EXCLUDED.engagement_rate, conversions = EXCLUDED.conversions,
                       total_revenue = EXCLUDED.total_revenue""",
                (
                    dk, channel_key,
                    int(r["sessions"]), int(r["totalUsers"]), int(r["newUsers"]), int(r["engagedSessions"]),
                    Decimal(r["engagementRate"]), int(Decimal(r["conversions"])), Decimal(r["totalRevenue"]),
                ),
            )
            loaded += 1
    return loaded


def load_page_rows(conn, rows: list[dict]) -> int:
    loaded = 0
    with conn.cursor() as cur:
        for r in rows:
            dk = int(r["date"])
            page_key = upsert_page(conn, page_path=r.get("pagePath"), page_title=r.get("pageTitle"))
            if page_key is None:
                continue
            users = int(r["totalUsers"])
            avg_engagement = (Decimal(r["userEngagementDuration"]) / users) if users else None
            cur.execute(
                """INSERT INTO analytics.fact_ga4_page_views_daily
                       (date_key, page_key, screen_page_views, total_users, avg_engagement_time_sec)
                   VALUES (%s,%s,%s,%s,%s)
                   ON CONFLICT (date_key, page_key) DO UPDATE SET
                       screen_page_views = EXCLUDED.screen_page_views,
                       total_users = EXCLUDED.total_users,
                       avg_engagement_time_sec = EXCLUDED.avg_engagement_time_sec""",
                (dk, page_key, int(r["screenPageViews"]), users, avg_engagement),
            )
            loaded += 1
    return loaded


def run() -> None:
    import db
    db.apply_schema()

    if not config.GA4_PROPERTY_ID:
        logger.warning("GA4_PROPERTY_ID not set - skipping GA4 ETL")
        return

    client = _client()
    with db.get_conn() as conn:
        with track_run(conn, "ga4", logger) as state:
            channel_rows = fetch_channel_report(client)
            page_rows = fetch_page_report(client)
            n1 = load_channel_rows(conn, channel_rows)
            n2 = load_page_rows(conn, page_rows)
            state["rows_loaded"] = n1 + n2
        conn.commit()


if __name__ == "__main__":
    run()
