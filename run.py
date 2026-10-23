"""Shop-Floor OS command line.

    python run.py context --zone FINAL # federated zone context from Zetaris + one Nemotron summary
    python run.py models               # model ids offered by Nightshift
"""

import argparse
import re
import time

from dotenv import load_dotenv

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
    started = time.perf_counter()
    rows = run_sql(CONTEXT_SQL.format(zone=zone.upper()))
    elapsed = time.perf_counter() - started
    print(" | ".join(COLUMNS))
    for row in rows:
        print(" | ".join("" if v is None else str(v) for v in row))
    print(f"{len(rows)} rows from Zetaris in {elapsed:.2f}s")

    table = "\n".join(", ".join(f"{c}={v}" for c, v in zip(COLUMNS, row)) for row in rows)
    reply = chat(
        "deep",
        [
            {"role": "system", "content": "You are a shift assistant in a car assembly plant. Answer in two sentences."},
            {"role": "user", "content": f"Recent incidents in zone {zone.upper()}:\n{table}\n\nWhat stands out?"},
        ],
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
