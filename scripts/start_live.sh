#!/usr/bin/env bash
# Start everything for a live demo, idempotently: check Zetaris, (re)create the Postgres container,
# load the data if it is missing, refresh Zetaris, check Nightshift, then start the web UI.
#   bash scripts/start_live.sh              # UI on http://127.0.0.1:${PORT:-8100}
#   bash scripts/start_live.sh --no-server  # everything except the UI
set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
PORT="${PORT:-8100}"

[ -f .env ] || { echo "Missing .env: copy .env.example to .env and fill in Zetaris and Nightshift settings"; exit 1; }
[ -x "$PY" ] || { echo "Missing .venv: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"; exit 1; }

echo "1/5 Zetaris containers"
if ! docker ps --format '{{.Names}}' | grep -q '^zetaris-api'; then
  echo "    Zetaris is not running. Start it from its install folder (e.g. cd ~/hackathon/zetaris-platform && docker compose up -d),"
  echo "    wait until 'docker ps' shows the zetaris-* containers as healthy, then run this script again."
  exit 1
fi
echo "    ok"

echo "2/5 Postgres (shopfloor-pg on the Zetaris network, host port 5433)"
bash scripts/start_postgres.sh

echo "3/5 Data"
if [ "$(docker exec shopfloor-pg psql -U postgres -d ops -Atc 'SELECT count(*) FROM incidents' 2>/dev/null || echo 0)" -gt 0 ]; then
  echo "    already loaded"
else
  bash scripts/load_data.sh
fi

echo "4/5 Zetaris datasources (agent logs synced into sf_ops.agent_steps and sf_ops.llm_calls first)"
$PY run.py logsync > /dev/null || echo "    log sync skipped"
if $PY -m app.zetaris "SHOW DATASOURCES" | grep -q sf_ops; then
  $PY -m app.zetaris refresh
else
  $PY -m app.zetaris register
fi

echo "5/5 Health"
$PY run.py health > logs/health.json
$PY - <<'PY'
import json
h = json.load(open("logs/health.json"))
for k in ("zetaris", "nightshift"):
    print(f"    {k}: {h[k]['status']}")
if h["nightshift"]["status"] != "ok":
    print("    Nightshift is not answering: start it, or run the demo offline with SAMPLE_MODE=true")
PY
if [ "${1:-}" = "--no-server" ]; then
  echo "Ready. Start the UI with: $PY -m app.server --port $PORT"
  exit 0
fi
# pick the first free port from $PORT (on WSL, Windows apps can hold ports too)
while (echo > "/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; do PORT=$((PORT + 1)); done
echo "Starting the UI on http://127.0.0.1:$PORT (Ctrl+C to stop)"
exec $PY -m app.server --port "$PORT"
