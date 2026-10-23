"""Shop-floor world model: our plant mapped onto the Meterless World Model engine.

One federated Zetaris query per zone (sf_ops, sf_hr, sf_fleet, sf_plant) returns one row per recent
incident. `build_world` turns those rows into a World Model:

    entities      incident, station, machine, worker, agv, vibration recording   (type:plant:id)
    context       the zone the incidents happened in                             (zone:plant:FINAL)
    relationships incident -occurred-at-> station, -handled-by-> worker, -involves-> agv,
                  -on-machine-> machine, -evidence-> recording; machine -located-at-> station
    facts         grounded checks, e.g. responder_loto_valid, agv_service_overdue

Every write carries provenance (kind "api", by "zetaris", at = incident time), so building twice from
the same rows gives the same canonical state. `build_context` is the spec's context builder (s.8.2):
it is intent-driven, prioritised and size-bounded, and returns a structured payload. That payload is
what the Nemotron prompt sees. `ground` lets a planner reject ids that are not in the world.
"""

from __future__ import annotations

import json
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

from app.engines.world_model import WorldModel, canonical_json, iso, sha256_hex

LOG_PATH = Path(__file__).resolve().parents[2] / "logs" / "world_model.jsonl"
SYSTEM = "plant"
AGV_SERVICE_DAYS = 28

ZONE_SQL = """
SELECT i.incident_id, i.ts, i.shift_code, i.kind, i.cause, i.zone_id, i.station_id, s.name AS station_name,
       i.machine_id, m.name AS machine_name, i.agv_id, i.severity, i.time_to_clear_s, i.downtime_min, i.cars_lost,
       i.cost_usd, i.outcome, i.false_alarm, i.description, i.worker_id, w.name AS worker_name, w.role AS worker_role,
       w.skills AS worker_skills, c.expires_date AS worker_loto_expires, a.model AS agv_model,
       a.battery_pct AS agv_battery_pct, a.faults_90d AS agv_faults_90d, a.last_service_date AS agv_last_service,
       i.vibration_recording, v.label AS vibration_label, v.x_rms AS vibration_x_rms
FROM sf_ops.incidents i
LEFT JOIN sf_plant.stations s ON s.station_id = i.station_id
LEFT JOIN sf_plant.machines m ON m.machine_id = i.machine_id
LEFT JOIN sf_hr.workers w ON w.worker_id = i.worker_id
LEFT JOIN sf_hr.certifications c ON c.worker_id = i.worker_id AND c.cert = 'lockout_tagout'
LEFT JOIN sf_fleet.agvs a ON a.agv_id = i.agv_id
LEFT JOIN sf_plant.vibration_samples v ON v.recording = i.vibration_recording
WHERE i.zone_id = '{zone}'
ORDER BY i.ts DESC
LIMIT {limit}
"""
ZONE_COLUMNS = ["incident_id", "ts", "shift_code", "kind", "cause", "zone_id", "station_id", "station_name",
                "machine_id", "machine_name", "agv_id", "severity", "time_to_clear_s", "downtime_min", "cars_lost",
                "cost_usd", "outcome", "false_alarm", "description", "worker_id", "worker_name", "worker_role",
                "worker_skills", "worker_loto_expires", "agv_model", "agv_battery_pct", "agv_faults_90d",
                "agv_last_service", "vibration_recording", "vibration_label", "vibration_x_rms"]
REPAIR_KINDS = ("robot_cell_fault", "machine_overheat")


def zone_query(zone: str, limit: int = 8) -> str:
    return ZONE_SQL.format(zone=zone, limit=int(limit))


def rows_to_dicts(rows: list[list[Any]]) -> list[dict]:
    return [dict(zip(ZONE_COLUMNS, row)) for row in rows]


def _num(value: Any) -> Any:
    if value in (None, ""):
        return None
    try:
        f = float(value)
        return int(f) if f.is_integer() else f
    except (TypeError, ValueError):
        return value


def _bool(value: Any) -> bool:
    return str(value).lower() in ("true", "t", "1")


def _ts(value: Any) -> str:
    """Zetaris returns '2026-10-16 21:49:37.0'; the world model keeps ISO strings."""
    return iso(datetime.fromisoformat(str(value).replace(" ", "T")))


def _day(value: Any) -> date | None:
    return date.fromisoformat(str(value)[:10]) if value not in (None, "") else None


def build_world(rows: list[dict], zone: str) -> WorldModel:
    """Build a World Model from the rows of one zone query. Deterministic: same rows, same state."""
    world = WorldModel(namespace=f"shopfloor-{zone.lower()}")
    if not rows:
        return world
    first_at = min(_ts(r["ts"]) for r in rows)
    zone_ctx = world.upsert_context("zone", external_key=(SYSTEM, zone), attrs={"zone_id": zone},
                                    source={"kind": "api", "by": "zetaris", "at": first_at, "url": "zetaris://sf_ops.zones"})["id"]
    for r in sorted(rows, key=lambda r: _ts(r["ts"])):
        at = _ts(r["ts"])
        src = {"kind": "api", "by": "zetaris", "at": at, "url": "zetaris://sf_ops.incidents"}
        incident = world.upsert_entity("incident", external_key=(SYSTEM, r["incident_id"]), name=r["incident_id"], source=src,
                                       attrs={"kind": r["kind"], "cause": r["cause"], "shift": r["shift_code"],
                                              "severity": r["severity"], "time_to_clear_s": _num(r["time_to_clear_s"]),
                                              "downtime_min": _num(r["downtime_min"]), "cars_lost": _num(r["cars_lost"]),
                                              "cost_usd": _num(r["cost_usd"]), "outcome": r["outcome"],
                                              "false_alarm": _bool(r["false_alarm"]), "description": r["description"],
                                              "at": at})["id"]
        if r.get("station_id"):
            station = world.upsert_entity("station", external_key=(SYSTEM, r["station_id"]), name=r["station_name"],
                                          attrs={"zone_id": r["zone_id"]}, source=src)["id"]
            world.relate(incident, "occurred-at", station, context=zone_ctx, source=src)
        if r.get("machine_id"):
            machine = world.upsert_entity("machine", external_key=(SYSTEM, r["machine_id"]), name=r["machine_name"],
                                          source=src)["id"]
            world.relate(incident, "on-machine", machine, context=zone_ctx, source=src)
            if r.get("station_id"):
                world.relate(machine, "located-at", f"station:{SYSTEM}:{r['station_id']}", source=src)
        if r.get("worker_id"):
            worker = world.upsert_entity("worker", external_key=(SYSTEM, r["worker_id"]), name=r["worker_name"], source=src,
                                         attrs={"role": r["worker_role"], "skills": sorted(str(r["worker_skills"] or "").split(",")),
                                                "loto_expires": str(r["worker_loto_expires"])[:10] if r["worker_loto_expires"] else None})["id"]
            world.relate(incident, "handled-by", worker, context=zone_ctx, source=src)
            if r["kind"] in REPAIR_KINDS and not _bool(r["false_alarm"]):
                expires = _day(r["worker_loto_expires"])
                valid = expires is not None and expires >= _day(r["ts"])
                world.assert_fact(incident, "responder_loto_valid", valid, confidence=1.0,
                                  source={**src, "kind": "pipeline", "by": "shopfloor-world"})
        if r.get("agv_id"):
            agv = world.upsert_entity("agv", external_key=(SYSTEM, r["agv_id"]), name=r["agv_id"], source=src,
                                      attrs={"model": r["agv_model"], "battery_pct": _num(r["agv_battery_pct"]),
                                             "faults_90d": _num(r["agv_faults_90d"]),
                                             "last_service": str(r["agv_last_service"])[:10] if r["agv_last_service"] else None})["id"]
            world.relate(incident, "involves", agv, context=zone_ctx, source=src)
            last = _day(r["agv_last_service"])
            if last:
                world.assert_fact(agv, "service_overdue", (_day(r["ts"]) - last).days > AGV_SERVICE_DAYS,
                                  confidence=1.0, source={**src, "kind": "pipeline", "by": "shopfloor-world"})
        if r.get("vibration_recording"):
            rec = world.upsert_entity("vibration_recording", external_key=("bosch-cnc", r["vibration_recording"]),
                                      name=r["vibration_recording"], source={**src, "url": "zetaris://sf_plant.vibration_samples"},
                                      attrs={"label": r["vibration_label"], "x_rms": _num(r["vibration_x_rms"])})["id"]
            world.relate(incident, "evidence", rec, context=zone_ctx, source=src)
    world.snapshot(max(_ts(r["ts"]) for r in rows), context=zone_ctx,
                   entities=[e["id"] for e in world.view("graph").query()],
                   source={"kind": "pipeline", "by": "shopfloor-world", "at": max(_ts(r["ts"]) for r in rows)})
    return world


def _compact(entity: dict, keep: tuple[str, ...]) -> dict:
    out = {"id": entity["id"], "name": entity.get("name")}
    out.update({k: entity["attrs"].get(k) for k in keep if entity["attrs"].get(k) is not None})
    return out


def build_context(world: WorldModel, zone: str, intent: str = "shift-briefing", max_chars: int = 6000) -> dict:
    """Context builder (spec s.8.2): select by zone, prioritise by recency then salience, size-bound.

    Returns a structured payload, not a pre-rendered string."""
    graph, facts = world.view("graph"), world.view("facts")
    incidents = sorted(graph.query(type="incident"), key=lambda e: e["attrs"].get("at", ""), reverse=True)
    items = []
    for inc in incidents:
        links = {}
        for via, key in (("occurred-at", "station"), ("on-machine", "machine"), ("handled-by", "worker"),
                         ("involves", "agv"), ("evidence", "vibration")):
            found = graph.outgoing(inc["id"], via=via)
            if found:
                links[key] = found[0]["id"]
        flags = {f["predicate"]: f["value"] for f in facts.about(inc["id"]) if not f.get("supersededBy")}
        items.append({**_compact(inc, ("at", "kind", "cause", "severity", "downtime_min", "cars_lost", "cost_usd",
                                       "outcome", "false_alarm", "description")), **links, **({"checks": flags} if flags else {})})

    def ranked(type_: str, keep: tuple[str, ...]) -> list[dict]:
        out = []
        for e in graph.query(type=type_):
            linked = len(graph.incoming(e["id"]))
            row = {**_compact(e, keep), "incidents_linked": linked}
            checks = {f["predicate"]: f["value"] for f in facts.about(e["id"]) if not f.get("supersededBy")}
            if checks:
                row["checks"] = checks
            out.append(row)
        return sorted(out, key=lambda x: (-x["incidents_linked"], x["id"]))  # salience first

    payload = {
        "intent": intent, "zone": zone, "engine": "meterless-world-model",
        "counts": {t: len(graph.query(type=t)) for t in ("incident", "station", "machine", "worker", "agv", "vibration_recording")},
        "incidents": items,
        "stations": ranked("station", ()),
        "machines": ranked("machine", ()),
        "workers": ranked("worker", ("role", "loto_expires")),
        "agvs": ranked("agv", ("model", "battery_pct", "faults_90d", "last_service")),
        "vibration": ranked("vibration_recording", ("label", "x_rms")),
    }
    while len(canonical_json(payload)) > max_chars and payload["incidents"]:  # truncate oldest incidents first
        payload["incidents"].pop()
        payload["truncated"] = True
    return payload


def ground(world: WorldModel, ids: list[str]) -> list[str]:
    """Return the ids that are NOT in the world model, so a planner can refuse invented entities."""
    graph = world.view("graph")
    return [i for i in ids if graph.entity(i) is None]


def log_step(step: str, zone: str, world: WorldModel, payload: dict, latency_s: float) -> None:
    """One JSONL line per agent step (AGENTS.md rule 7). No secrets are written."""
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    record = {"ts": datetime.now().astimezone().isoformat(), "agent": "world-model", "step": step, "zone": zone,
              "entities": payload.get("counts"), "events": len(world.events()), "audit": len(world.audit()),
              "state_hash": sha256_hex(world.state_shape_canonical())[:16],
              "latency_s": round(latency_s, 4)}
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


def zone_world(zone: str, run_sql, limit: int = 8) -> tuple[WorldModel, dict, list[dict], float]:
    """The feature entry point: one Zetaris query -> world model -> context payload."""
    started = time.perf_counter()
    rows = rows_to_dicts(run_sql(zone_query(zone, limit)))
    query_s = time.perf_counter() - started
    world = build_world(rows, zone)
    payload = build_context(world, zone)
    log_step("build-context", zone, world, payload, time.perf_counter() - started)
    return world, payload, rows, query_s
