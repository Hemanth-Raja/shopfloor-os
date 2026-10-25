"""Plant-wide world model for the agent loop, built from ONE federated Zetaris query per incident.

The query is a UNION ALL over sf_plant (stations, machines), sf_hr (workers, certifications), sf_fleet
(AGVs with their latest telemetry position), and sf_ops (safety rules, per-kind cost history). Every row
has the same columns: (row_kind, id, name, x, y, a, b, c, d). The rows become World Model entities
(station, machine, worker, agv) with positions, certifications and health, plus the safety rules and
historical cost stats that the critic and the restraint gate read.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.engines.world_model import WorldModel

SYSTEM = "plant"
AGV_SERVICE_DAYS = 28

PLANT_SQL = """
SELECT 'station' AS row_kind, station_id AS id, name, x, y, zone_id AS a, CAST(kitted AS STRING) AS b,
       CAST(buffer_min AS STRING) AS c, NULL AS d FROM sf_plant.stations
UNION ALL
SELECT 'machine', machine_id, name, x, y, station_id, kind, zone_id, NULL FROM sf_plant.machines
UNION ALL
SELECT 'worker', worker_id, name, x, y, role, shift_code, skills, station_id FROM sf_hr.workers
UNION ALL
SELECT 'cert', worker_id, cert, NULL, NULL, CAST(expires_date AS STRING), NULL, NULL, NULL FROM sf_hr.certifications
UNION ALL
SELECT 'agv', a.agv_id, a.model, t.x, t.y, a.kind, CAST(a.battery_health_pct AS STRING),
       CAST(a.faults_90d AS STRING), CAST(a.last_service_date AS STRING)
FROM sf_fleet.agvs a JOIN sf_fleet.agv_telemetry t ON t.agv_id = a.agv_id
WHERE t.ts = (SELECT max(ts) FROM sf_fleet.agv_telemetry)
UNION ALL
SELECT 'rule', rule_id, cfr_ref, NULL, NULL, check_type, params, summary, NULL FROM sf_ops.safety_rules
UNION ALL
SELECT 'stat', kind, NULL, NULL, NULL, CAST(avg(cost_usd) AS STRING), CAST(avg(cars_lost) AS STRING),
       CAST(avg(downtime_min) AS STRING), CAST(count(*) AS STRING) FROM sf_ops.incidents GROUP BY kind
"""
COLUMNS = ["row_kind", "id", "name", "x", "y", "a", "b", "c", "d"]


def _int(v: Any) -> int | None:
    return None if v in (None, "") else int(float(v))


def _float(v: Any) -> float | None:
    return None if v in (None, "") else float(v)


class PlantWorld:
    """A World Model of the plant plus the side tables the agents need (rules, cost stats)."""

    def __init__(self, rows: list[list[Any]] | list[dict], today: date, at: str):
        recs = [r if isinstance(r, dict) else dict(zip(COLUMNS, r)) for r in rows]
        self.today = today
        self.world = WorldModel(namespace="plant")
        self.rules: dict[str, dict] = {}
        self.stats: dict[str, dict] = {}
        src = {"kind": "api", "by": "zetaris", "at": at, "url": "zetaris://plant-query"}
        certs: dict[str, dict[str, str]] = {}
        for r in recs:
            if r["row_kind"] == "cert":
                certs.setdefault(r["id"], {})[r["name"]] = str(r["a"])[:10]
            elif r["row_kind"] == "rule":
                self.rules[r["id"]] = {"cfr_ref": r["name"], "check_type": r["a"], "params": r["b"], "summary": r["c"]}
            elif r["row_kind"] == "stat":
                self.stats[r["id"]] = {"avg_cost_usd": _float(r["a"]), "avg_cars_lost": _float(r["b"]),
                                       "avg_downtime_min": _float(r["c"]), "count": _int(r["d"])}
        for r in sorted(recs, key=lambda r: (r["row_kind"], str(r["id"]))):
            kind = r["row_kind"]
            if kind == "station":
                self.world.upsert_entity("station", external_key=(SYSTEM, r["id"]), name=r["name"], source=src,
                                         attrs={"x": _int(r["x"]), "y": _int(r["y"]), "zone_id": r["a"],
                                                "kitted": str(r["b"]).lower() == "true", "buffer_min": _int(r["c"])})
            elif kind == "machine":
                self.world.upsert_entity("machine", external_key=(SYSTEM, r["id"]), name=r["name"], source=src,
                                         attrs={"x": _int(r["x"]), "y": _int(r["y"]), "station_id": r["a"],
                                                "kind": r["b"], "zone_id": r["c"]})
                self.world.relate(f"machine:{SYSTEM}:{r['id']}", "located-at", f"station:{SYSTEM}:{r['a']}", source=src)
            elif kind == "worker":
                expiry = certs.get(r["id"], {})
                self.world.upsert_entity("worker", external_key=(SYSTEM, r["id"]), name=r["name"], source=src,
                                         attrs={"x": _int(r["x"]), "y": _int(r["y"]), "role": r["a"], "shift": r["b"],
                                                "skills": sorted(s for s in str(r["c"] or "").split(",") if s),
                                                "stations": r["d"], "cert_expiry": dict(sorted(expiry.items()))})
            elif kind == "agv":
                last = date.fromisoformat(str(r["d"])[:10]) if r["d"] else None
                overdue = last is not None and (today - last).days > AGV_SERVICE_DAYS
                agv_id = self.world.upsert_entity("agv", external_key=(SYSTEM, r["id"]), name=r["id"], source=src,
                                                  attrs={"x": _int(r["x"]), "y": _int(r["y"]), "model": r["name"],
                                                         "kind": r["a"], "battery_health_pct": _int(r["b"]),
                                                         "faults_90d": _int(r["c"]),
                                                         "last_service": str(r["d"])[:10] if r["d"] else None})["id"]
                self.world.assert_fact(agv_id, "service_overdue", overdue, confidence=1.0,
                                       source={**src, "kind": "pipeline", "by": "plant-world"})

    # ---- helpers the agents use

    def entities(self, type_: str) -> list[dict]:
        return self.world.view("graph").query(type=type_)

    def entity(self, id_: str) -> dict | None:
        return self.world.view("graph").entity(id_)

    def cert_valid(self, worker: dict, cert: str) -> bool:
        expiry = worker["attrs"].get("cert_expiry", {}).get(cert)
        return expiry is not None and date.fromisoformat(expiry) >= self.today

    def station_xy(self, station_id: str) -> tuple[int, int]:
        st = self.entity(f"station:{SYSTEM}:{station_id}")
        if not st:
            raise KeyError(f"unknown station {station_id}")
        return st["attrs"]["x"], st["attrs"]["y"]


def load_plant_world(run_sql, today: date, at: str) -> PlantWorld:
    """One federated read-only query -> PlantWorld, stamped with a version (hash of the rows) and its load time,
    so every decision can say which snapshot of the plant it saw."""
    import hashlib
    import json
    import time
    rows = run_sql(PLANT_SQL)
    world = PlantWorld(rows, today, at)
    world.version = hashlib.sha256(json.dumps(rows, default=str, sort_keys=True).encode()).hexdigest()[:12]
    world.loaded_at = time.time()
    return world
