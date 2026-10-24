"""Candidates: top-3 workers and AGVs for an incident, from the plant world model (one Zetaris query).

The ranking is by distance from the agent's current sim position, the matching skill, a valid
certification and AGV health. An AGV whose service is overdue is never offered for a critical task.
"""

from __future__ import annotations

from app.contracts import Candidate, Incident

# incident kind -> (skills that qualify a worker, certification that must be valid or None)
WORKER_NEEDS = {
    "part_shortage": ({"tugger_driving", "andon_response"}, None),
    "agv_fault": ({"agv_reset"}, None),
    "overheat": ({"machine_repair"}, "lockout_tagout"),
    "spill": ({"spill_response"}, None),
}
NEEDS_AGV = {"part_shortage", "agv_fault"}
CRITICAL = {"high", "critical"}


def _facts(plant, entity_id: str) -> dict:
    return {f["predicate"]: f["value"] for f in plant.world.view("facts").about(entity_id) if not f.get("supersededBy")}


def find_candidates(plant, sim, incident: Incident, k: int = 3, exclude: set[str] | None = None) -> dict:
    exclude = exclude or set()
    skills, cert = WORKER_NEEDS[incident.kind]
    workers: list[Candidate] = []
    for e in plant.entities("worker"):
        agent = sim.agents.get(e["id"])
        if agent is None or e["id"] in exclude:
            continue  # not on shift in the sim, or excluded after a failure
        have = set(e["attrs"]["skills"])
        if not have & skills:
            continue
        certs = {c: plant.cert_valid(e, c) for c in sorted(e["attrs"].get("cert_expiry", {}))}
        if cert and not certs.get(cert, False):
            continue
        dist = abs(agent.pos[0] - incident.x) + abs(agent.pos[1] - incident.y)
        score = round(100 - dist - (0 if have & skills else 20), 1)
        workers.append(Candidate(id=e["id"], type="worker", name=e["name"], distance=dist, skills=sorted(have),
                                 certs_valid=certs, score=score,
                                 reasons=[f"{dist} cells away", f"skills {sorted(have & skills)}"]
                                 + ([f"{cert} valid"] if cert else [])))
    agvs: list[Candidate] = []
    if incident.kind in NEEDS_AGV:
        for e in plant.entities("agv"):
            agent = sim.agents.get(e["id"])
            if agent is None or e["id"] in exclude or e["id"] == incident.agv_id or agent.status in ("fault", "stalled"):
                continue
            if e["attrs"].get("kind") != "kitting":
                continue
            overdue = bool(_facts(plant, e["id"]).get("service_overdue"))
            if overdue and incident.severity in CRITICAL:
                continue
            dist = abs(agent.pos[0] - incident.x) + abs(agent.pos[1] - incident.y)
            health = {"battery_health_pct": e["attrs"].get("battery_health_pct"),
                      "faults_90d": e["attrs"].get("faults_90d"), "service_overdue": overdue}
            score = round(100 - dist + 0.2 * (health["battery_health_pct"] or 0) - 0.3 * (health["faults_90d"] or 0), 1)
            agvs.append(Candidate(id=e["id"], type="agv", name=e["name"], distance=dist, health=health, score=score,
                                  reasons=[f"{dist} cells away", f"battery health {health['battery_health_pct']}%",
                                           f"{health['faults_90d']} faults in 90 days"]))
    return {"workers": sorted(workers, key=lambda c: (-c.score, c.id))[:k],
            "agvs": sorted(agvs, key=lambda c: (-c.score, c.id))[:k]}
