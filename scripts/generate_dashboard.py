#!/usr/bin/env python
"""Generate a self-contained HTML revenue dashboard from
analytics.fact_manual_transactions.

Queries Postgres once, aggregates in Python, and embeds the result as JSON
in a static HTML file (Chart.js from CDN for rendering) - the page itself
makes no DB connection and can be opened, emailed, or served as a plain
static file.

Prerequisite: the analytics schema must already exist and have data in it
(run scripts/sheets_to_analytics.py or run_all.py first).

Usage (run on a host that can reach Postgres, e.g. the VPS itself):
    python scripts/generate_dashboard.py                    # writes ./dashboard.html
    python scripts/generate_dashboard.py --output /opt/lorchidee-etl/dashboard.html
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from psycopg.rows import dict_row

import config
import db

QUERY = """
    SELECT
        f.date_key,
        dd.date,
        COALESCE(s.service_name, 'Unknown') AS service_name,
        COALESCE(pm.payment_method_name, 'Unknown') AS payment_method_name,
        f.price_amount,
        f.product_sold_amount,
        f.currency
    FROM analytics.fact_manual_transactions f
    LEFT JOIN analytics.dim_date dd ON dd.date_key = f.date_key
    LEFT JOIN analytics.dim_service s ON s.service_key = f.service_key
    LEFT JOIN analytics.dim_payment_method pm ON pm.payment_method_key = f.payment_method_key
    ORDER BY f.date_key
"""

TOP_N_SERVICES = 8
MAX_PIE_SLICES = 6


def fetch_rows() -> list[dict]:
    with db.get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(QUERY)
            return cur.fetchall()


def aggregate(rows: list[dict]) -> dict:
    total_revenue = Decimal("0")
    by_date: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    by_service: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    by_payment_method: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    currency_counts: Counter[str] = Counter()

    for r in rows:
        # "Revenue" = the service charge plus any product upsell on the same
        # transaction - the two amounts the sheet actually tracks per row.
        amount = (r["price_amount"] or Decimal("0")) + (r["product_sold_amount"] or Decimal("0"))
        total_revenue += amount
        date_label = r["date"].isoformat() if r["date"] else str(r["date_key"])
        by_date[date_label] += amount
        by_service[r["service_name"]] += amount
        by_payment_method[r["payment_method_name"]] += amount
        currency_counts[r["currency"] or "CAD"] += 1

    n = len(rows)
    currency = currency_counts.most_common(1)[0][0] if currency_counts else "CAD"
    avg_transaction = (total_revenue / n) if n else Decimal("0")

    revenue_by_date = [{"date": d, "revenue": float(v)} for d, v in sorted(by_date.items())]

    services_sorted = sorted(by_service.items(), key=lambda kv: kv[1], reverse=True)
    revenue_by_service = [{"service": k, "revenue": float(v)} for k, v in services_sorted[:TOP_N_SERVICES]]

    pm_sorted = sorted(by_payment_method.items(), key=lambda kv: kv[1], reverse=True)
    if len(pm_sorted) > MAX_PIE_SLICES:
        head, tail = pm_sorted[: MAX_PIE_SLICES - 1], pm_sorted[MAX_PIE_SLICES - 1 :]
        pm_sorted = head + [("Other", sum((v for _, v in tail), Decimal("0")))]
    revenue_by_payment_method = [{"method": k, "revenue": float(v)} for k, v in pm_sorted]

    return {
        "currency": currency,
        "total_revenue": float(total_revenue),
        "total_transactions": n,
        "avg_transaction": float(avg_transaction),
        "revenue_by_date": revenue_by_date,
        "revenue_by_service": revenue_by_service,
        "revenue_by_payment_method": revenue_by_payment_method,
    }


def render_html(data: dict, generated_at: str) -> str:
    # `</` inside the JSON (e.g. a service/payment-method name) would
    # otherwise prematurely close the <script> tag it's embedded in.
    data_json = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html = _TEMPLATE.replace("__DASHBOARD_DATA_JSON__", data_json).replace("__GENERATED_AT__", generated_at)
    return html


_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>L'Orchidee - Revenue Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
<style>
  :root {
    color-scheme: light;
    --page:            #f9f9f7;
    --surface-1:       #fcfcfb;
    --text-primary:    #0b0b0b;
    --text-secondary:  #52514e;
    --text-muted:      #898781;
    --gridline:        #e1e0d9;
    --baseline:        #c3c2b7;
    --border:          rgba(11,11,11,0.10);
    --series-1:        #2a78d6;
    --series-2:        #eb6834;
    --series-3:        #1baf7a;
    --series-4:        #eda100;
    --series-5:        #e87ba4;
    --series-6:        #4a3aa7;
    --series-other:    #898781;
  }
  @media (prefers-color-scheme: dark) {
    :root:where(:not([data-theme="light"])) {
      color-scheme: dark;
      --page:            #0d0d0d;
      --surface-1:       #1a1a19;
      --text-primary:    #ffffff;
      --text-secondary:  #c3c2b7;
      --text-muted:      #898781;
      --gridline:        #2c2c2a;
      --baseline:        #383835;
      --border:          rgba(255,255,255,0.10);
      --series-1:        #3987e5;
      --series-2:        #d95926;
      --series-3:        #199e70;
      --series-4:        #c98500;
      --series-5:        #d55181;
      --series-6:        #9085e9;
      --series-other:    #898781;
    }
  }

  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--page);
    color: var(--text-primary);
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  .wrap { max-width: 1120px; margin: 0 auto; padding: 32px 24px 64px; }

  header { margin-bottom: 28px; }
  h1 { font-size: 22px; font-weight: 600; margin: 0 0 4px; }
  .subtitle { color: var(--text-secondary); font-size: 13px; }

  .stat-row {
    display: grid;
    grid-template-columns: 1.4fr 1fr 1fr;
    gap: 16px;
    margin-bottom: 24px;
  }
  .card {
    background: var(--surface-1);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 20px 22px;
  }
  .stat-label { font-size: 13px; color: var(--text-secondary); margin-bottom: 8px; }
  .stat-value { font-size: 32px; font-weight: 600; line-height: 1.1; font-variant-numeric: proportional-nums; }
  .stat-row .card:not(.hero) .stat-value { font-size: 22px; }

  .chart-card { padding: 22px 22px 16px; margin-bottom: 20px; }
  .chart-title { font-size: 14px; font-weight: 600; margin: 0 0 2px; }
  .chart-subtitle { font-size: 12px; color: var(--text-muted); margin: 0 0 14px; }
  .chart-card canvas { max-width: 100%; }

  .grid-2 { display: grid; grid-template-columns: 1.4fr 1fr; gap: 20px; }

  footer { color: var(--text-muted); font-size: 12px; margin-top: 8px; }

  @media (max-width: 760px) {
    .stat-row { grid-template-columns: 1fr; }
    .grid-2 { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>L'Orchidee &mdash; Revenue Dashboard</h1>
    <div class="subtitle">Source: analytics.fact_manual_transactions &middot; generated __GENERATED_AT__</div>
  </header>

  <div class="stat-row">
    <div class="card hero">
      <div class="stat-label">Total revenue</div>
      <div class="stat-value" id="stat-total-revenue">-</div>
    </div>
    <div class="card">
      <div class="stat-label">Transactions</div>
      <div class="stat-value" id="stat-total-transactions">-</div>
    </div>
    <div class="card">
      <div class="stat-label">Average transaction</div>
      <div class="stat-value" id="stat-avg-transaction">-</div>
    </div>
  </div>

  <div class="card chart-card">
    <p class="chart-title">Revenue over time</p>
    <p class="chart-subtitle">Daily total, service charges + product upsells</p>
    <canvas id="chart-revenue-time" height="90"></canvas>
  </div>

  <div class="grid-2">
    <div class="card chart-card">
      <p class="chart-title">Top services by revenue</p>
      <p class="chart-subtitle">Highest-earning services, total revenue</p>
      <canvas id="chart-top-services" height="240"></canvas>
    </div>
    <div class="card chart-card">
      <p class="chart-title">Payment method breakdown</p>
      <p class="chart-subtitle">Share of total revenue</p>
      <canvas id="chart-payment-methods" height="240"></canvas>
    </div>
  </div>

  <footer>L'Orchidee ETL &middot; scripts/generate_dashboard.py</footer>
</div>

<script>
const DATA = __DASHBOARD_DATA_JSON__;

const root = getComputedStyle(document.documentElement);
const token = (name) => root.getPropertyValue(name).trim();
const COLORS = {
  textSecondary: token('--text-secondary'),
  textMuted: token('--text-muted'),
  gridline: token('--gridline'),
  baseline: token('--baseline'),
  surface: token('--surface-1'),
  series: [
    token('--series-1'), token('--series-2'), token('--series-3'),
    token('--series-4'), token('--series-5'), token('--series-6'),
  ],
  other: token('--series-other'),
};

const money = (value) => new Intl.NumberFormat('en-CA', {
  style: 'currency', currency: DATA.currency || 'CAD', maximumFractionDigits: 0,
}).format(value);

document.getElementById('stat-total-revenue').textContent = money(DATA.total_revenue);
document.getElementById('stat-total-transactions').textContent =
  new Intl.NumberFormat('en-CA').format(DATA.total_transactions);
document.getElementById('stat-avg-transaction').textContent = money(DATA.avg_transaction);

Chart.defaults.font.family = "system-ui, -apple-system, 'Segoe UI', sans-serif";
Chart.defaults.color = COLORS.textSecondary;
Chart.defaults.borderColor = COLORS.gridline;

const axisGrid = { color: COLORS.gridline, drawTicks: false };
const axisTicks = { color: COLORS.textMuted, font: { size: 11 } };

new Chart(document.getElementById('chart-revenue-time'), {
  type: 'line',
  data: {
    labels: DATA.revenue_by_date.map(d => d.date),
    datasets: [{
      label: 'Revenue',
      data: DATA.revenue_by_date.map(d => d.revenue),
      borderColor: COLORS.series[0],
      backgroundColor: COLORS.series[0] + '1a',
      borderWidth: 2,
      pointRadius: 4,
      pointBackgroundColor: COLORS.series[0],
      pointBorderColor: COLORS.surface,
      pointBorderWidth: 2,
      tension: 0.15,
      fill: true,
    }],
  },
  options: {
    responsive: true,
    plugins: {
      legend: { display: false },
      tooltip: { callbacks: { label: (ctx) => money(ctx.parsed.y) } },
    },
    scales: {
      x: { grid: { display: false }, ticks: axisTicks },
      y: { grid: axisGrid, border: { display: false }, ticks: { ...axisTicks, callback: (v) => money(v) }, beginAtZero: true },
    },
  },
});

new Chart(document.getElementById('chart-top-services'), {
  type: 'bar',
  data: {
    labels: DATA.revenue_by_service.map(d => d.service),
    datasets: [{
      label: 'Revenue',
      data: DATA.revenue_by_service.map(d => d.revenue),
      backgroundColor: COLORS.series[0],
      borderRadius: 4,
      maxBarThickness: 24,
      categoryPercentage: 0.7,
    }],
  },
  options: {
    indexAxis: 'y',
    responsive: true,
    plugins: {
      legend: { display: false },
      tooltip: { callbacks: { label: (ctx) => money(ctx.parsed.x) } },
    },
    scales: {
      x: { grid: axisGrid, border: { display: false }, ticks: { ...axisTicks, callback: (v) => money(v) }, beginAtZero: true },
      y: { grid: { display: false }, ticks: axisTicks },
    },
  },
});

const pmColors = DATA.revenue_by_payment_method.map((d, i) =>
  d.method === 'Other' ? COLORS.other : COLORS.series[i % COLORS.series.length]
);
new Chart(document.getElementById('chart-payment-methods'), {
  type: 'pie',
  data: {
    labels: DATA.revenue_by_payment_method.map(d => d.method),
    datasets: [{
      data: DATA.revenue_by_payment_method.map(d => d.revenue),
      backgroundColor: pmColors,
      borderColor: COLORS.surface,
      borderWidth: 2,
    }],
  },
  options: {
    responsive: true,
    plugins: {
      legend: { position: 'bottom', labels: { boxWidth: 12, padding: 14 } },
      tooltip: {
        callbacks: {
          label: (ctx) => {
            const total = ctx.dataset.data.reduce((a, b) => a + b, 0);
            const pct = total ? ((ctx.parsed / total) * 100).toFixed(1) : '0.0';
            return `${ctx.label}: ${money(ctx.parsed)} (${pct}%)`;
          },
        },
      },
    },
  },
});
</script>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--output", default=str(config.BASE_DIR / "dashboard.html"),
        help="Output HTML path (default: dashboard.html next to this project, e.g. /opt/lorchidee-etl/dashboard.html on the VPS).",
    )
    args = parser.parse_args()

    print("Querying analytics.fact_manual_transactions...")
    rows = fetch_rows()
    print(f"Fetched {len(rows)} transactions")

    data = aggregate(rows)
    from datetime import datetime, timezone
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    html = render_html(data, generated_at)

    output_path = Path(args.output)
    output_path.write_text(html, encoding="utf-8")

    print(f"Wrote {output_path} ({len(html):,} bytes)")
    print(
        f"  total revenue: {data['currency']} {data['total_revenue']:,.2f} "
        f"across {data['total_transactions']} transactions"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
