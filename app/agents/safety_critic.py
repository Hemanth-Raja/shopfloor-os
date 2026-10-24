"""Safety critic: a deterministic rule engine over sf_ops.safety_rules. No model calls.

Checks: certification validity (lockout/tagout, forklift, robot-cell entry), safe speed, pedestrian
crossing speed of 0.5 m/s or less, minimum separation from the incident area, and no AGV routed through a
blocked spill zone. Violations carry the rule id and CFR reference from the rules table.
"""

from __future__ import annotations

from app.contracts import CriticVerdict, Incident, Plan, Violation
from app.agents.executor import agv_legs
from app.sim.pathing import plan_path

MAX_SPEED_MPS = 1.5
CROSSING_SPEED_MPS = 0.5
MIN_SEPARATION_CELLS = 2


def review(plan: Plan, incident: Incident, plant, sim) -> CriticVerdict:
    rules = plant.rules
    ref = lambda rid: rules.get(rid, {}).get("cfr_ref", "plant rule")  # noqa: E731
    out: list[Violation] = []

    for wid in plan.assignee_ids:
        w = plant.entity(wid)
        if not w or w["type"] != "worker":
            continue
        name = f"{wid} ({w['name']})"
        if incident.kind == "overheat" and not plant.cert_valid(w, "lockout_tagout"):
            exp = w["attrs"].get("cert_expiry", {}).get("lockout_tagout", "none")
            out.append(Violation(rule_id="R-LOTO", cfr_ref=ref("R-LOTO"),
                                 detail=f"{name} lockout/tagout not valid (expires {exp}) for machine repair"))
        if incident.zone_id in ("BODY", "PAINT") and not plant.cert_valid(w, "robot_cell_entry"):
            out.append(Violation(rule_id="R-ROBOT-CELL", cfr_ref=ref("R-ROBOT-CELL"),
                                 detail=f"{name} has no valid robot-cell entry card for {incident.zone_id}"))
        if incident.kind == "part_shortage" and "tugger_driving" in w["attrs"]["skills"] \
                and not plant.cert_valid(w, "forklift"):
            out.append(Violation(rule_id="R-TRAIN", cfr_ref=ref("R-TRAIN"),
                                 detail=f"{name} forklift/tugger training not valid"))

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
