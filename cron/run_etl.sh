#!/usr/bin/env bash
# Wrapper for cron: activates the venv, runs the full ETL, logs stdout/stderr.
# Deploy this whole `lorchidee-etl` folder to the VPS, then run once:
#   python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
# and install the crontab entry from cron/crontab.example.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

source .venv/bin/activate

mkdir -p logs
exec python run_all.py >> "logs/cron_$(date +\%Y-\%m-\%d).log" 2>&1
