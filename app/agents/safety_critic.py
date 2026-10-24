"""Safety critic: a deterministic rule engine over sf_ops.safety_rules. No model calls.

Checks: certification validity (lockout/tagout, forklift, robot-cell entry), safe speed, pedestrian
crossing speed of 0.5 m/s or less, minimum separation from the incident area, and no AGV routed through a
blocked spill zone. Violations carry the rule id and CFR reference from the rules table.
"""

from __future__ import annotations

from app.agents.candidates import MUST_HAVE
from app.contracts import Citation, CriticVerdict, Incident, Plan, Violation
from app.agents.executor import agv_legs
from app.sim.pathing import plan_path

MAX_SPEED_MPS = 1.5
CROSSING_SPEED_MPS = 0.5
MIN_SEPARATION_CELLS = 2
CELL_ENTRY_KINDS = {"robot_cell_fault", "overheat"}  # the responder steps into the robot cell or machine guarding


def review(plan: Plan, incident: Incident, plant, sim, citations=None) -> CriticVerdict:
    """Rule checks; with a citation index, every violation carries the exact OSHA clause and plant SOP."""
    verdict = _review(plan, incident, plant, sim)
    if citations is not None:
        for v in verdict.violations:
            v.citations = [Citation(**c) for c in citations.for_rule(v.rule_id, v.detail)]
    return verdict


def _review(plan: Plan, incident: Incident, plant, sim) -> CriticVerdict:
    rules = plant.rules
    ref = lambda rid: rules.get(rid, {}).get("cfr_ref", "plant rule")  # noqa: E731
    out: list[Violation] = []

    for wid in plan.assignee_ids:
        w = plant.entity(wid)
        if not w or w["type"] != "worker":
            continue
        name = f"{wid} ({w['name']})"
        if incident.kind in ("overheat", "robot_cell_fault") and not plant.cert_valid(w, "lockout_tagout"):
            exp = w["attrs"].get("cert_expiry", {}).get("lockout_tagout", "none")
            out.append(Violation(rule_id="R-LOTO", cfr_ref=ref("R-LOTO"),
                                 detail=f"{name} lockout/tagout not valid (expires {exp}) for "
                                        f"{'machine repair' if incident.kind == 'overheat' else 'robot cell recovery'}"))
        if incident.zone_id in ("BODY", "PAINT") and incident.kind in CELL_ENTRY_KINDS \
                and not plant.cert_valid(w, "robot_cell_entry"):
            out.append(Violation(rule_id="R-ROBOT-CELL", cfr_ref=ref("R-ROBOT-CELL"),
                                 detail=f"{name} has no valid robot-cell entry card for {incident.zone_id}"))
        if incident.kind == "part_shortage" and "tugger_driving" in w["attrs"]["skills"] \
                and not plant.cert_valid(w, "forklift"):
            out.append(Violation(rule_id="R-TRAIN", cfr_ref=ref("R-TRAIN"),
                                 detail=f"{name} forklift/tugger training not valid"))

    if incident.kind in MUST_HAVE:
        rule_id, need, why = MUST_HAVE[incident.kind]
        people = [plant.entity(i) for i in plan.assignee_ids]
        if not any(p and p["type"] == "worker" and need & set(p["attrs"]["skills"]) for p in people):
            out.append(Violation(rule_id=rule_id, cfr_ref=ref(rule_id),
                                 detail=f"{why}: assign someone with one of the skills {sorted(need)}"))
    if incident.kind == "near_miss" and incident.agv_id and any(r.agv_id == incident.agv_id for r in plan.agv_routes):
        out.append(Violation(rule_id="R-NEAR-MISS", cfr_ref=ref("R-NEAR-MISS"),
                             detail=f"{incident.agv_id} was in the near miss and stays stopped until a supervisor releases it"))

    for route in plan.agv_routes:
        agv = sim.agents.get(route.agv_id)
        if route.speed_mps > MAX_SPEED_MPS:
            out.append(Violation(rule_id="R-SAFE-SPEED", cfr_ref=ref("R-SAFE-SPEED"),
                                 detail=f"{route.agv_id} speed {route.speed_mps} m/s exceeds {MAX_SPEED_MPS} m/s"))
        try:
            plant.station_xy(route.to_station)
        except KeyError:
            out.append(Violation(rule_id="GROUNDING", cfr_ref="World Model", detail=f"unknown station {route.to_station}"))
            continue
        if agv is None:
            continue
        pickup, target = agv_legs(route, plant, sim)  # supermarket pickup, then line-side delivery
        path = (plan_path(sim.grid, agv.pos, pickup, 0) or []) + (plan_path(sim.grid, pickup, target, 0) or [])
        crossing_speed = route.crossing_speed_mps if route.crossing_speed_mps is not None else route.speed_mps
        crosses = sorted({sim.grid.label(*c) for c in path if sim.grid.is_crossing(*c)})
        if crosses and crossing_speed > CROSSING_SPEED_MPS:
            out.append(Violation(rule_id="R-CROSSING", cfr_ref=ref("R-CROSSING"),
                                 detail=f"{route.agv_id} crosses {', '.join(crosses)} at {crossing_speed} m/s; set "
                                        f"crossing_speed_mps <= {CROSSING_SPEED_MPS}"))
        fence = sim.geofence
        if fence and any(abs(c[0] - f[0]) + abs(c[1] - f[1]) < MIN_SEPARATION_CELLS for c in [target] for f in fence):
            rid = "R-SURFACE" if incident.kind == "spill" else "R-PED-CLEAR"
            out.append(Violation(rule_id=rid, cfr_ref=ref(rid),
                                 detail=f"{route.agv_id} would stop within {MIN_SEPARATION_CELLS} cells of the fenced incident area"))
    return CriticVerdict(ok=not out, violations=out)
