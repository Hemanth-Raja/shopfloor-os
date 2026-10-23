#!/usr/bin/env bash
# Generate the data and (re)load it into the four source databases.
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m app.data.generate
for db in fleet hr plant ops; do
  docker exec shopfloor-pg psql -U postgres -q -c "DROP DATABASE IF EXISTS $db" -c "CREATE DATABASE $db"
  docker exec -i shopfloor-pg psql -U postgres -d "$db" -q -v ON_ERROR_STOP=1 < "data/sql/$db.sql"
  echo "loaded $db"
done
docker exec shopfloor-pg psql -U postgres -d ops -c "SELECT kind, count(*) FROM incidents GROUP BY kind ORDER BY kind"
