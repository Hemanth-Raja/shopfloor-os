#!/usr/bin/env bash
# Generate the data and (re)load it into the four source databases.
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m app.data.generate
# Keep lab experiment proposals across reloads: save the rows before ops is recreated, restore after.
EXPERIMENTS=$(docker exec shopfloor-pg pg_dump -U postgres -d ops --data-only --table=experiments 2>/dev/null || true)
for db in fleet hr plant ops; do
  docker exec shopfloor-pg psql -U postgres -q -c "DROP DATABASE IF EXISTS $db WITH (FORCE)" -c "CREATE DATABASE $db"
  docker exec -i shopfloor-pg psql -U postgres -d "$db" -q -v ON_ERROR_STOP=1 < "data/sql/$db.sql"
  echo "loaded $db"
done
if [ -n "$EXPERIMENTS" ]; then
  printf '%s\n' "$EXPERIMENTS" | docker exec -i shopfloor-pg psql -U postgres -d ops -q -v ON_ERROR_STOP=1
  echo "kept $(docker exec shopfloor-pg psql -U postgres -d ops -At -c 'SELECT count(*) FROM experiments') experiment(s)"
fi
docker exec shopfloor-pg psql -U postgres -d ops -c "SELECT kind, count(*) FROM incidents GROUP BY kind ORDER BY kind"
