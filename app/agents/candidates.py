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
    "robot_cell_fault": ({"machine_repair", "robot_cell_entry"}, None),  # the critic checks LOTO and the cell card
    "torque_fault": ({"torque_tools", "torque_audit", "quality_inspection"}, None),
    "near_miss": ({"incident_command", "first_aid"}, None),
    "quality_defect": ({"quality_inspection", "torque_audit", "paint_inspection", "weld_inspection"}, None),
}
# incident kind -> (rule id, skills at least one assignee must have, why); checked by the safety critic
MUST_HAVE = {
    "torque_fault": ("R-TORQUE-QA", {"torque_audit", "quality_inspection"},
                     "joints fastened since the last good torque must be re-audited"),
    "near_miss": ("R-NEAR-MISS", {"incident_command"}, "a supervisor with incident command must secure the scene"),
    "quality_defect": ("R-CONTAIN", {"quality_inspection"}, "a quality inspector must contain the affected units"),
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
            if agent is None or e["id"] in exclude or e["id"] == incident.agv_id or agent.status in ("fault", "stalled", "held"):
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
    workers = sorted(workers, key=lambda c: (-c.score, c.id))
    top = workers[:k]
    if incident.kind in MUST_HAVE and top:  # always offer at least one person who can satisfy the critic
        need = MUST_HAVE[incident.kind][1]
        if not any(need & set(c.skills) for c in top):
            best = next((c for c in workers if need & set(c.skills)), None)
            if best:
                top = top[:k - 1] + [best]
    return {"workers": top,
            "agvs": sorted(agvs, key=lambda c: (-c.score, c.id))[:k]}
