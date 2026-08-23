#!/usr/bin/env bash
# Run on the VPS after code is rsynced to /opt/lorchidee-etl (see
# .github/workflows/deploy.yml). Idempotent: safe to run on every deploy.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

if [ ! -d .venv ]; then
    python3 -m venv .venv
fi
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

chmod +x cron/run_etl.sh
mkdir -p logs secrets

if [ ! -f .env ]; then
    echo "WARNING: .env not found at $PROJECT_DIR/.env - copy .env.example there and fill in real values." >&2
fi
if [ ! -f secrets/service_account.json ]; then
    echo "WARNING: secrets/service_account.json not found - Google API ETLs will fail until it's added." >&2
fi

# Install/refresh the cron entry without clobbering any of the user's other
# cron jobs: strip any previous line referencing this project, then append
# the current crontab.example block.
( crontab -l 2>/dev/null | grep -v "lorchidee-etl/cron/run_etl.sh" || true; cat cron/crontab.example ) | crontab -

echo "Deploy complete: venv installed, cron installed."
