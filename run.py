"""Shop-Floor OS command line.

    python run.py context --zone FINAL # one Zetaris query -> Meterless World Model -> Nemotron summary
    python run.py models               # model ids offered by Nightshift
"""

import argparse
import json
import re

from dotenv import load_dotenv

from app.engines.shopfloor_world import zone_world
from app.llm import chat, list_models
from app.zetaris import run_sql

load_dotenv()

CONTEXT_SQL = """
SELECT i.incident_id, i.ts, i.kind, i.station_id, i.description, i.severity, i.time_to_clear_s,
       i.downtime_min, i.cars_lost, i.cost_usd, i.outcome, i.worker_id, w.name AS worker_name, w.skills AS worker_skills,
       i.agv_id, a.battery_pct AS agv_battery_pct
FROM sf_ops.incidents i
LEFT JOIN sf_hr.workers w ON w.worker_id = i.worker_id
LEFT JOIN sf_fleet.agvs a ON a.agv_id = i.agv_id
WHERE i.zone_id = '{zone}'
ORDER BY i.ts DESC
LIMIT 5
"""
COLUMNS = ["incident_id", "ts", "kind", "station_id", "description", "severity", "time_to_clear_s",
           "downtime_min", "cars_lost", "cost_usd", "outcome", "worker_id", "worker_name", "worker_skills", "agv_id", "agv_battery_pct"]


def context(zone):
    if not re.fullmatch(r"[A-Za-z0-9-]+", zone):
        raise SystemExit("zone must be a zone id such as FINAL, TRIM or X-EAST")
    world, payload, rows, elapsed = zone_world(zone.upper(), run_sql)
    print(" | ".join(COLUMNS))
    for r in rows[:5]:
        print(" | ".join("" if r.get(c) is None else str(r.get(c)) for c in COLUMNS))
    print(f"{len(rows)} rows from Zetaris in {elapsed:.2f}s (one federated query)")
    counts = ", ".join(f"{n} {t}" for t, n in payload["counts"].items() if n)
    checks = [f"{e['id']}: {e['checks']}" for k in ("incidents", "agvs") for e in payload[k] if e.get("checks")]
    print(f"World model: {counts}; {len(world.events())} events, {len(world.audit())} audit entries")
    for line in checks:
        print(f"  check {line}")

    # The prompt sees world-model state (entities, links, grounded checks), not raw rows.
    reply = chat(
        "deep",
        [
            {"role": "system", "content": "You are a shift assistant in a car assembly plant. Use only the entities "
                                          "and checks in the world-model JSON; do not invent ids. Answer in two sentences."},
            {"role": "user", "content": f"World model for zone {zone.upper()}:\n{json.dumps(payload)}\n\nWhat stands out?"},
        ],
        max_tokens=2000,  # the deep lane reasons before answering; 800 cut replies short
    )
    print(f"\nNemotron ({reply.model}):\n{reply.text}")


def models():
    for model_id in list_models():
        print(model_id)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Shop-Floor OS")
    sub = parser.add_subparsers(dest="command", required=True)
    ctx = sub.add_parser("context", help="zone context from Zetaris plus a Nemotron summary")
    ctx.add_argument("--zone", default="FINAL")
    sub.add_parser("models", help="list model ids offered by Nightshift")
    args = parser.parse_args()
    context(args.zone) if args.command == "context" else models()
