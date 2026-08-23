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

def fetch_rows() -> list[dict]:
    with db.get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(QUERY)
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

  <div class="filter-row">
    <div class="filter-field">
      <label for="start-date">Start date</label>
      <input type="date" id="start-date">
    </div>
    <div class="filter-field">
      <label for="end-date">End date</label>
      <input type="date" id="end-date">
    </div>
    <div class="filter-presets" id="filter-presets">
      <button type="button" data-preset="7">Last 7 days</button>
      <button type="button" data-preset="30">Last 30 days</button>
      <button type="button" data-preset="90">Last 90 days</button>
      <button type="button" data-preset="all">All time</button>
    </div>
  </div>

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

  <p class="empty-state" id="empty-state" hidden>No transactions in this date range.</p>

  <div class="card chart-card">
    <p class="chart-title">Revenue over time</p>
    <p class="chart-subtitle">Daily total, service charges + product upsells</p>
    <div class="chart-canvas-wrap"><canvas id="chart-revenue-time"></canvas></div>
  </div>

  <div class="grid-2">
    <div class="card chart-card">
      <p class="chart-title">Top services by revenue</p>
      <p class="chart-subtitle">Highest-earning services, total revenue</p>
      <div class="chart-canvas-wrap tall"><canvas id="chart-top-services"></canvas></div>
    </div>
    <div class="card chart-card">
      <p class="chart-title">Payment method breakdown</p>
      <p class="chart-subtitle">Share of total revenue</p>
      <div class="chart-canvas-wrap tall"><canvas id="chart-payment-methods"></canvas></div>
    </div>
  </div>

  <footer>L'Orchidee ETL &middot; scripts/generate_dashboard.py</footer>
</div>

<script>
const DATA = __DASHBOARD_DATA_JSON__;
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

function filterTransactions(start, end) {
  return DATA.transactions.filter((t) => (!start || t.date >= start) && (!end || t.date <= end));
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
      label: 'Revenue',
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
      label: 'Revenue',
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

const emptyState = document.getElementById('empty-state');

function render(start, end) {
  const agg = computeAggregates(filterTransactions(start, end));

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
}

// --- date-range picker: two native <input type="date"> (no external
// library - the OS/browser supplies the calendar UI), plus quick presets ---
const startInput = document.getElementById('start-date');
const endInput = document.getElementById('end-date');
const presetButtons = document.querySelectorAll('#filter-presets button');

if (DATA.min_date) { startInput.min = DATA.min_date; endInput.min = DATA.min_date; }
if (DATA.max_date) { startInput.max = DATA.max_date; endInput.max = DATA.max_date; }
startInput.value = DATA.min_date || '';
endInput.value = DATA.max_date || '';

function syncConstraints() {
  // keep the native picker from allowing an inverted range
  startInput.max = endInput.value || DATA.max_date || '';
  endInput.min = startInput.value || DATA.min_date || '';
}

function setActivePreset(preset) {
  presetButtons.forEach((btn) => btn.classList.toggle('active', btn.dataset.preset === preset));
}

function renderFromInputs() {
  render(startInput.value || null, endInput.value || null);
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

presetButtons.forEach((btn) => btn.addEventListener('click', () => {
  const preset = btn.dataset.preset;
  if (preset === 'all' || !DATA.max_date) {
    startInput.value = DATA.min_date || '';
    endInput.value = DATA.max_date || '';
  } else {
    const end = new Date(DATA.max_date + 'T00:00:00');
    const start = new Date(end);
    start.setDate(start.getDate() - (Number(preset) - 1));
    const minDate = DATA.min_date ? new Date(DATA.min_date + 'T00:00:00') : start;
    startInput.value = (start < minDate ? minDate : start).toISOString().slice(0, 10);
    endInput.value = DATA.max_date;
  }
  syncConstraints();
  setActivePreset(preset);
  renderFromInputs();
}));

syncConstraints();
setActivePreset('all');
renderFromInputs();
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
