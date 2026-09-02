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
from collections import Counter
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

# etl/ga4_etl.py writes analytics.fact_ga4_traffic_daily at (date, channel)
# grain - summed across channels here since the dashboard only needs a daily
# sessions/users total, not a channel breakdown. Table always exists (schema
# creates it regardless of whether GA4 has run yet), so an empty/no-rows
# result - GA4 not configured, or genuinely zero traffic - is the normal
# case to handle, not an error.
#
# date_key is a plain int in YYYYMMDD form (e.g. 20260824), parsed directly
# with TO_DATE rather than joined against dim_date - a join that doesn't
# match silently drops the row (build_ga4_data skips rows with no date),
# which is exactly what was producing "0 sessions / 0 users" despite the
# table having data.
GA4_QUERY = """
    SELECT
        TO_DATE(f.date_key::text, 'YYYYMMDD') AS date,
        SUM(f.sessions) AS sessions,
        SUM(f.total_users) AS total_users
    FROM analytics.fact_ga4_traffic_daily f
    GROUP BY f.date_key
    ORDER BY f.date_key
"""


# public.bookings lives in the same lorchidee_bookings database as the
# analytics schema, so this reuses the one connection - no second DSN needed.
# Cancelled rows are excluded outright: a cancellation isn't a no-show, and
# counting it either way would skew the rate.
BOOKINGS_QUERY = """
    SELECT
        status,
        COUNT(*) AS count
    FROM public.bookings
    WHERE cancelled = false
    GROUP BY status
"""


def fetch_rows() -> list[dict]:
    with db.get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(QUERY)
            return cur.fetchall()


def fetch_ga4_rows() -> list[dict]:
    with db.get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(GA4_QUERY)
            return cur.fetchall()


def fetch_bookings_rows() -> list[dict]:
    with db.get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(BOOKINGS_QUERY)
            return cur.fetchall()


def build_dashboard_data(rows: list[dict]) -> dict:
    """Emit transaction-level records rather than pre-aggregated summaries:
    the date-range picker filters and re-aggregates client-side (in JS, see
    _TEMPLATE), so every stat/chart needs the raw rows to slice, not just
    one fixed all-time rollup."""
    currency_counts: Counter[str] = Counter()
    transactions = []

    for r in rows:
        if r["date"] is None:
            continue  # can't place on a date axis or filter by date without one
        # "Revenue" = the service charge plus any product upsell on the same
        # transaction - the two amounts the sheet actually tracks per row.
        amount = (r["price_amount"] or Decimal("0")) + (r["product_sold_amount"] or Decimal("0"))
        currency_counts[r["currency"] or "CAD"] += 1
        transactions.append(
            {
                "date": r["date"].isoformat(),
                "service": r["service_name"],
                "payment_method": r["payment_method_name"],
                "revenue": float(amount),
            }
        )

    currency = currency_counts.most_common(1)[0][0] if currency_counts else "CAD"
    dates = sorted(t["date"] for t in transactions)

    return {
        "currency": currency,
        "transactions": transactions,
        "min_date": dates[0] if dates else None,
        "max_date": dates[-1] if dates else None,
    }


def build_ga4_data(rows: list[dict]) -> list[dict]:
    """One entry per day with GA4 data - empty list if the table has no rows
    yet (GA4 not configured, or genuinely no traffic), which the dashboard
    renders as zeroed stats rather than an error."""
    out = []
    for r in rows:
        if r["date"] is None:
            continue
        out.append(
            {
                "date": r["date"].isoformat(),
                "sessions": int(r["sessions"] or 0),
                "users": int(r["total_users"] or 0),
            }
        )
    return out


def build_bookings_data(rows: list[dict]) -> dict:
    """Counts per status, as a fixed all-time rollup - unlike the transaction
    stats these aren't sliced by the date-range picker, since the query groups
    by status rather than by date and has nothing to filter client-side."""
    counts = {(r["status"] or ""): int(r["count"] or 0) for r in rows}
    confirmed = counts.get("confirmed", 0)
    no_show = counts.get("no_show", 0)
    total = confirmed + no_show
    return {
        "confirmed": confirmed,
        "no_show": no_show,
        "total": total,
        "no_show_rate": (no_show / total * 100) if total else 0.0,
    }


def render_html(data: dict, generated_at: str) -> str:
    # `</` inside the JSON (e.g. a service/payment-method name) would
    # otherwise prematurely close the <script> tag it's embedded in.
    data_json = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    bookings_json = json.dumps(data.get("bookings") or {}, ensure_ascii=False).replace("</", "<\\/")
    html = (
        _TEMPLATE
        .replace("__DASHBOARD_DATA_JSON__", data_json)
        .replace("__BOOKINGS_DATA_JSON__", bookings_json)
        .replace("__GENERATED_AT__", generated_at)
    )
    return html


_TEMPLATE = r"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>L'Orchidée — Tableau de bord</title>
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
  /* Elements toggled via the `hidden` DOM property must actually hide:
     author display rules (e.g. .stat-row's `display: grid`) otherwise beat
     the UA default [hidden]{display:none} at equal specificity. */
  [hidden] { display: none !important; }
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

  .filter-row {
    display: flex;
    align-items: flex-end;
    flex-wrap: wrap;
    gap: 20px;
    margin-bottom: 20px;
    padding-bottom: 20px;
    border-bottom: 1px solid var(--border);
  }
  .filter-field { display: flex; flex-direction: column; gap: 6px; }
  .filter-field label { font-size: 12px; color: var(--text-secondary); }
  .filter-field input[type="date"] {
    font: inherit;
    font-size: 13px;
    color: var(--text-primary);
    background: var(--surface-1);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 7px 10px;
    min-height: 34px;
    color-scheme: inherit;
  }
  .filter-field input[type="date"]:focus-visible {
    outline: 2px solid var(--series-1);
    outline-offset: 1px;
  }
  .filter-presets { display: flex; flex-wrap: wrap; gap: 8px; margin-left: auto; }
  .filter-presets button {
    font: inherit;
    font-size: 12px;
    color: var(--text-secondary);
    background: transparent;
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 7px 12px;
    min-height: 34px;
    cursor: pointer;
  }
  .filter-presets button:hover { background: var(--gridline); }
  .filter-presets button.active {
    color: var(--text-primary);
    border-color: var(--series-1);
    font-weight: 600;
  }

  .empty-state {
    text-align: center;
    color: var(--text-muted);
    font-size: 13px;
    padding: 32px 0;
  }

  .stat-row {
    display: grid;
    grid-template-columns: 1.4fr 1fr 1fr;
    gap: 16px;
    margin-bottom: 24px;
  }
  .stat-row-2 { grid-template-columns: 1fr 1fr; }
  .stat-row-2 .stat-value { font-size: 22px; }

  .section-title {
    font-size: 15px;
    font-weight: 600;
    margin: 36px 0 16px;
    padding-top: 20px;
    border-top: 1px solid var(--border);
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
  /* Fixed-height, explicitly-positioned wrapper so Chart.js's responsive
     resize has a stable box to measure - a canvas resizing against an
     un-sized flex/grid parent (or its own aspect-ratio height) can enter a
     grow-every-update feedback loop otherwise. maintainAspectRatio:false
     in each chart's options pairs with this. */
  .chart-canvas-wrap { position: relative; width: 100%; height: 260px; }
  .chart-canvas-wrap.tall { height: 320px; }

  .grid-2 { display: grid; grid-template-columns: 1.4fr 1fr; gap: 20px; }

  footer { color: var(--text-muted); font-size: 12px; margin-top: 8px; }

  @media (max-width: 760px) {
    .stat-row, .stat-row-2 { grid-template-columns: 1fr; }
    .grid-2 { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>L'Orchidée &mdash; Tableau de bord</h1>
    <div class="subtitle">Source : analytics.fact_manual_transactions &middot; généré le __GENERATED_AT__</div>
  </header>

  <div class="filter-row">
    <div class="filter-field">
      <label for="start-date">Date de début</label>
      <input type="date" id="start-date">
    </div>
    <div class="filter-field">
      <label for="end-date">Date de fin</label>
      <input type="date" id="end-date">
    </div>
    <div class="filter-presets" id="filter-presets">
      <button type="button" data-preset="7">7 derniers jours</button>
      <button type="button" data-preset="30">30 derniers jours</button>
      <button type="button" data-preset="90">90 derniers jours</button>
      <button type="button" data-preset="all">Tout afficher</button>
    </div>
  </div>

  <div class="stat-row">
    <div class="card hero">
      <div class="stat-label">Revenus totaux</div>
      <div class="stat-value" id="stat-total-revenue">-</div>
    </div>
    <div class="card">
      <div class="stat-label">Transactions</div>
      <div class="stat-value" id="stat-total-transactions">-</div>
    </div>
    <div class="card">
      <div class="stat-label">Transaction moyenne</div>
      <div class="stat-value" id="stat-avg-transaction">-</div>
    </div>
  </div>

  <p class="empty-state" id="empty-state" hidden>Aucune transaction dans cette plage de dates.</p>

  <div class="card chart-card">
    <p class="chart-title">Revenus dans le temps</p>
    <p class="chart-subtitle">Total quotidien, services + ventes de produits</p>
    <div class="chart-canvas-wrap"><canvas id="chart-revenue-time"></canvas></div>
  </div>

  <div class="grid-2">
    <div class="card chart-card">
      <p class="chart-title">Services les plus rentables</p>
      <p class="chart-subtitle">Services générant le plus de revenus</p>
      <div class="chart-canvas-wrap tall"><canvas id="chart-top-services"></canvas></div>
    </div>
    <div class="card chart-card">
      <p class="chart-title">Modes de paiement</p>
      <p class="chart-subtitle">Répartition des revenus par mode de paiement</p>
      <div class="chart-canvas-wrap tall"><canvas id="chart-payment-methods"></canvas></div>
    </div>
  </div>

  <h2 class="section-title">Trafic web (GA4)</h2>
  <p class="empty-state" id="ga4-empty-state" hidden>
    Aucune donnée GA4 pour l'instant — source non configurée ou rien chargé dans analytics.fact_ga4_traffic_daily.
  </p>

  <div class="stat-row stat-row-2" id="ga4-stat-row">
    <div class="card">
      <div class="stat-label">Sessions totales</div>
      <div class="stat-value" id="stat-ga4-sessions">-</div>
    </div>
    <div class="card">
      <div class="stat-label">Utilisateurs totaux</div>
      <div class="stat-value" id="stat-ga4-users">-</div>
    </div>
  </div>

  <div class="card chart-card" id="ga4-chart-card">
    <p class="chart-title">Sessions dans le temps</p>
    <p class="chart-subtitle">Sessions quotidiennes, tous canaux</p>
    <div class="chart-canvas-wrap"><canvas id="chart-ga4-sessions"></canvas></div>
  </div>

  <h2 class="section-title">Réservations en ligne</h2>

  <div class="stat-row">
    <div class="card">
      <div class="stat-label">Réservations confirmées</div>
      <div class="stat-value" id="stat-bookings-confirmed">-</div>
    </div>
    <div class="card">
      <div class="stat-label">Non-présentations</div>
      <div class="stat-value" id="stat-bookings-no-show">-</div>
    </div>
    <div class="card">
      <div class="stat-label">Taux de non-présentation</div>
      <div class="stat-value" id="stat-bookings-no-show-rate">-</div>
    </div>
  </div>

  <footer>L'Orchidée ETL &middot; scripts/generate_dashboard.py</footer>
</div>

<script>
const DATA = __DASHBOARD_DATA_JSON__;
const BOOKINGS = __BOOKINGS_DATA_JSON__;
const TOP_N_SERVICES = 8;
const MAX_PIE_SLICES = 6;

const root = getComputedStyle(document.documentElement);
const token = (name) => root.getPropertyValue(name).trim();
const COLORS = {
  textSecondary: token('--text-secondary'),
  textMuted: token('--text-muted'),
  gridline: token('--gridline'),
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

// --- client-side aggregation, so the date-range picker can filter and
// re-render every chart from the same raw transaction list in real time ---
function computeAggregates(transactions) {
  let totalRevenue = 0;
  const byDate = new Map();
  const byService = new Map();
  const byPaymentMethod = new Map();

  for (const t of transactions) {
    totalRevenue += t.revenue;
    byDate.set(t.date, (byDate.get(t.date) || 0) + t.revenue);
    byService.set(t.service, (byService.get(t.service) || 0) + t.revenue);
    byPaymentMethod.set(t.payment_method, (byPaymentMethod.get(t.payment_method) || 0) + t.revenue);
  }

  const n = transactions.length;

  const revenueByDate = [...byDate.entries()]
    .sort((a, b) => (a[0] < b[0] ? -1 : 1))
    .map(([date, revenue]) => ({ date, revenue }));

  const revenueByService = [...byService.entries()]
    .sort((a, b) => b[1] - a[1])
    .slice(0, TOP_N_SERVICES)
    .map(([service, revenue]) => ({ service, revenue }));

  let pmSorted = [...byPaymentMethod.entries()].sort((a, b) => b[1] - a[1]);
  if (pmSorted.length > MAX_PIE_SLICES) {
    const head = pmSorted.slice(0, MAX_PIE_SLICES - 1);
    const otherSum = pmSorted.slice(MAX_PIE_SLICES - 1).reduce((sum, [, v]) => sum + v, 0);
    pmSorted = [...head, ['Other', otherSum]];
  }
  const revenueByPaymentMethod = pmSorted.map(([method, revenue]) => ({ method, revenue }));

  return {
    totalRevenue,
    totalTransactions: n,
    avgTransaction: n ? totalRevenue / n : 0,
    revenueByDate,
    revenueByService,
    revenueByPaymentMethod,
  };
}

function filterByDateRange(records, start, end) {
  return records.filter((r) => (!start || r.date >= start) && (!end || r.date <= end));
}

function computeGa4Aggregates(ga4Days) {
  let totalSessions = 0;
  let totalUsers = 0;
  const sessionsByDate = ga4Days.map((d) => {
    totalSessions += d.sessions;
    totalUsers += d.users;
    return { date: d.date, sessions: d.sessions };
  });
  return { totalSessions, totalUsers, sessionsByDate };
}

Chart.defaults.font.family = "system-ui, -apple-system, 'Segoe UI', sans-serif";
Chart.defaults.color = COLORS.textSecondary;
Chart.defaults.borderColor = COLORS.gridline;

const axisGrid = { color: COLORS.gridline, drawTicks: false };
const axisTicks = { color: COLORS.textMuted, font: { size: 11 } };

const revenueTimeChart = new Chart(document.getElementById('chart-revenue-time'), {
  type: 'line',
  data: {
    labels: [],
    datasets: [{
      label: 'Revenus',
      data: [],
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
    maintainAspectRatio: false,
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

const topServicesChart = new Chart(document.getElementById('chart-top-services'), {
  type: 'bar',
  data: {
    labels: [],
    datasets: [{
      label: 'Revenus',
      data: [],
      backgroundColor: COLORS.series[0],
      borderRadius: 4,
      maxBarThickness: 24,
      categoryPercentage: 0.7,
    }],
  },
  options: {
    indexAxis: 'y',
    responsive: true,
    maintainAspectRatio: false,
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

const paymentMethodsChart = new Chart(document.getElementById('chart-payment-methods'), {
  type: 'pie',
  data: {
    labels: [],
    datasets: [{ data: [], backgroundColor: [], borderColor: COLORS.surface, borderWidth: 2 }],
  },
  options: {
    responsive: true,
    maintainAspectRatio: false,
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

const ga4SessionsChart = new Chart(document.getElementById('chart-ga4-sessions'), {
  type: 'line',
  data: {
    labels: [],
    datasets: [{
      label: 'Sessions',
      data: [],
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
    maintainAspectRatio: false,
    plugins: {
      legend: { display: false },
      tooltip: { callbacks: { label: (ctx) => new Intl.NumberFormat('en-CA').format(ctx.parsed.y) } },
    },
    scales: {
      x: { grid: { display: false }, ticks: axisTicks },
      y: { grid: axisGrid, border: { display: false }, ticks: axisTicks, beginAtZero: true },
    },
  },
});

const emptyState = document.getElementById('empty-state');

// GA4 section: whether the table has ANY data at all is checked once (not
// per date-range change) - an empty filtered slice is a normal "no traffic
// this range" result, but an empty table overall means the source isn't
// wired up yet, which gets its own message instead of a misleadingly bare
// "0 sessions" stat row.
const ga4HasAnyData = DATA.ga4_daily.length > 0;
document.getElementById('ga4-empty-state').hidden = ga4HasAnyData;
document.getElementById('ga4-stat-row').hidden = !ga4HasAnyData;
document.getElementById('ga4-chart-card').hidden = !ga4HasAnyData;

// Online bookings: an all-time rollup, so it renders once here rather than
// inside render() - the date-range picker doesn't apply to it.
const countFmt = new Intl.NumberFormat('en-CA');
const rateFmt = new Intl.NumberFormat('en-CA', { minimumFractionDigits: 1, maximumFractionDigits: 1 });
document.getElementById('stat-bookings-confirmed').textContent = countFmt.format(BOOKINGS.confirmed || 0);
document.getElementById('stat-bookings-no-show').textContent = countFmt.format(BOOKINGS.no_show || 0);
document.getElementById('stat-bookings-no-show-rate').textContent =
  rateFmt.format(BOOKINGS.no_show_rate || 0) + ' %';

function render(start, end) {
  const agg = computeAggregates(filterByDateRange(DATA.transactions, start, end));

  emptyState.hidden = agg.totalTransactions > 0;

  document.getElementById('stat-total-revenue').textContent = money(agg.totalRevenue);
  document.getElementById('stat-total-transactions').textContent =
    new Intl.NumberFormat('en-CA').format(agg.totalTransactions);
  document.getElementById('stat-avg-transaction').textContent = money(agg.avgTransaction);

  revenueTimeChart.data.labels = agg.revenueByDate.map((d) => d.date);
  revenueTimeChart.data.datasets[0].data = agg.revenueByDate.map((d) => d.revenue);
  revenueTimeChart.update();

  topServicesChart.data.labels = agg.revenueByService.map((d) => d.service);
  topServicesChart.data.datasets[0].data = agg.revenueByService.map((d) => d.revenue);
  topServicesChart.update();

  const pmColors = agg.revenueByPaymentMethod.map((d, i) =>
    d.method === 'Other' ? COLORS.other : COLORS.series[i % COLORS.series.length]
  );
  paymentMethodsChart.data.labels = agg.revenueByPaymentMethod.map((d) => d.method);
  paymentMethodsChart.data.datasets[0].data = agg.revenueByPaymentMethod.map((d) => d.revenue);
  paymentMethodsChart.data.datasets[0].backgroundColor = pmColors;
  paymentMethodsChart.update();

  if (ga4HasAnyData) {
    const ga4Agg = computeGa4Aggregates(filterByDateRange(DATA.ga4_daily, start, end));
    document.getElementById('stat-ga4-sessions').textContent =
      new Intl.NumberFormat('en-CA').format(ga4Agg.totalSessions);
    document.getElementById('stat-ga4-users').textContent =
      new Intl.NumberFormat('en-CA').format(ga4Agg.totalUsers);
    ga4SessionsChart.data.labels = ga4Agg.sessionsByDate.map((d) => d.date);
    ga4SessionsChart.data.datasets[0].data = ga4Agg.sessionsByDate.map((d) => d.sessions);
    ga4SessionsChart.update();
  }
}

// --- date-range picker: two native <input type="date"> (no external
// library - the OS/browser supplies the calendar UI), plus quick presets ---
const startInput = document.getElementById('start-date');
const endInput = document.getElementById('end-date');
const presetButtons = document.querySelectorAll('#filter-presets button');

// "Today" is the viewer's local today (not the date the dashboard was
// generated on, and not the last transaction's date) - computed from local
// date parts, not toISOString(), which is UTC and can land on the wrong
// calendar day depending on the viewer's timezone.
function todayLocalISODate() {
  const d = new Date();
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}
const todayStr = todayLocalISODate();

// Selectable range spans every date in the data (transactions and GA4
// combined - GA4 rows are often more recent than the last manual
// transaction, which is exactly what was hiding recent GA4 data before),
// widened to include today so "today" is always pickable even on days
// with no data yet.
const allDataDates = [...DATA.transactions.map((t) => t.date), ...DATA.ga4_daily.map((d) => d.date)];
const earliestDataDate = allDataDates.length ? allDataDates.reduce((a, b) => (a < b ? a : b)) : todayStr;
const latestDataDate = allDataDates.length ? allDataDates.reduce((a, b) => (a > b ? a : b)) : todayStr;
const pickerMin = earliestDataDate < todayStr ? earliestDataDate : todayStr;
const pickerMax = latestDataDate > todayStr ? latestDataDate : todayStr;

startInput.min = pickerMin;
startInput.max = pickerMax;
endInput.min = pickerMin;
endInput.max = pickerMax;

function syncConstraints() {
  // keep the native picker from allowing an inverted range
  startInput.max = endInput.value || pickerMax;
  endInput.min = startInput.value || pickerMin;
}

function setActivePreset(preset) {
  presetButtons.forEach((btn) => btn.classList.toggle('active', btn.dataset.preset === preset));
}

function renderFromInputs() {
  render(startInput.value || null, endInput.value || null);
}

function applyPreset(preset) {
  if (preset === 'all') {
    startInput.value = pickerMin;
    endInput.value = pickerMax;
  } else {
    const end = new Date(todayStr + 'T00:00:00');
    const start = new Date(end);
    start.setDate(start.getDate() - (Number(preset) - 1));
    const minDate = new Date(pickerMin + 'T00:00:00');
    startInput.value = (start < minDate ? minDate : start).toISOString().slice(0, 10);
    endInput.value = todayStr;
  }
  syncConstraints();
  setActivePreset(preset);
  renderFromInputs();
}

startInput.addEventListener('input', () => {
  syncConstraints();
  setActivePreset(null);
  renderFromInputs();
});
endInput.addEventListener('input', () => {
  syncConstraints();
  setActivePreset(null);
  renderFromInputs();
});

presetButtons.forEach((btn) => btn.addEventListener('click', () => applyPreset(btn.dataset.preset)));

// Default view: last 90 days ending today (clamped to the earliest data
// date if there isn't 90 days of history yet) - same as clicking "Last 90
// days" - rather than the old default of the last transaction's date
// range, which hid recent GA4-only data.
applyPreset('90');
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

    data = build_dashboard_data(rows)
    skipped = len(rows) - len(data["transactions"])
    if skipped:
        print(f"  skipped {skipped} row(s) with no resolvable date (can't be date-filtered)")

    print("Querying analytics.fact_ga4_traffic_daily...")
    ga4_rows = fetch_ga4_rows()
    data["ga4_daily"] = build_ga4_data(ga4_rows)
    print(f"  {len(data['ga4_daily'])} day(s) of GA4 data" + ("" if data["ga4_daily"] else " (table is empty)"))

    print("Querying public.bookings...")
    bookings_rows = fetch_bookings_rows()
    data["bookings"] = build_bookings_data(bookings_rows)
    print(
        f"  {data['bookings']['confirmed']} confirmed, "
        f"{data['bookings']['no_show']} no-show "
        f"({data['bookings']['total']} total, {data['bookings']['no_show_rate']:.1f}% no-show rate)"
    )

    from datetime import datetime, timezone
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    html = render_html(data, generated_at)

    output_path = Path(args.output)
    output_path.write_text(html, encoding="utf-8")

    total_revenue = sum(t["revenue"] for t in data["transactions"])
    print(f"Wrote {output_path} ({len(html):,} bytes)")
    print(
        f"  total revenue: {data['currency']} {total_revenue:,.2f} "
        f"across {len(data['transactions'])} transactions "
        f"({data['min_date']} to {data['max_date']})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
