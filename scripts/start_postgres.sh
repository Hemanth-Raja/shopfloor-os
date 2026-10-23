#!/usr/bin/env bash
# Start the Shop-Floor OS Postgres on the same Docker network as Zetaris,
# so Zetaris can reach it at shopfloor-pg:5432. Safe to re-run.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a

NET="${ZETARIS_DOCKER_NETWORK:-$(docker network ls --format '{{.Name}}' | grep -i zetaris | head -1)}"
if [ -z "$NET" ]; then echo "No Zetaris Docker network found. Set ZETARIS_DOCKER_NETWORK in .env"; exit 1; fi

if docker ps -a --format '{{.Names}}' | grep -qx shopfloor-pg; then
  docker start shopfloor-pg >/dev/null
else
  docker run -d --name shopfloor-pg --network "$NET" -p 5433:5432 \
    -e POSTGRES_PASSWORD="$PG_PASSWORD" -v shopfloor-pgdata:/var/lib/postgresql/data postgres:16 >/dev/null
fi
until docker exec shopfloor-pg pg_isready -U postgres >/dev/null 2>&1; do sleep 1; done
echo "shopfloor-pg is up on network $NET (host port 5433)"
