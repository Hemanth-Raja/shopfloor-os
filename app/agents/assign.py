"""Anytime planning, step 1: a deterministic plan in milliseconds by optimal assignment. No model calls.

Each incident kind needs a few *slots* (e.g. torque fault: a tool technician and a torque auditor; part shortage: a
tugger driver and a kitting AGV). Every slot/candidate pair gets a cost:

    travel time (A* steps on the floor, 2 s each) + reliability (AGV faults, battery health)
    + load (already moving: busy) ; infeasible (missing skill, lapsed certificate, LOTO, robot-cell card,
    forklift training) = BIG

and the Hungarian algorithm (Kuhn-Munkres) picks the assignment with the lowest total cost. AGVs are routed at
1.0 m/s and 0.5 m/s at crossings, so the plan is built to pass the safety critic; it still goes through the critic
and the dry run like any other plan. The model's plan (app/dynamic.py, anytime refinement) only replaces it when
it is safe and better.
"""

from __future__ import annotations

from app.agents.candidates import MUST_HAVE, WORKER_NEEDS
from app.contracts import Incident, Plan

BIG = 1e6
SEVERITY_PRIORITY = {"low": "low", "medium": "medium", "high": "high", "critical": "critical"}
# kind -> slots: (slot type, required skills (any), required valid certificates (all))
SLOTS = {
    "part_shortage": [("worker", {"tugger_driving", "andon_response"}, set()), ("agv", None, None)],
    "agv_fault": [("worker", {"agv_reset"}, set()), ("agv", None, None)],
    "overheat": [("worker", {"machine_repair"}, {"lockout_tagout"})],
    "spill": [("worker", {"spill_response"}, set())],
    "robot_cell_fault": [("worker", {"machine_repair", "robot_cell_entry"}, {"lockout_tagout", "robot_cell_entry"})],
    "torque_fault": [("worker", {"torque_tools"}, set()), ("worker", MUST_HAVE["torque_fault"][1], set())],
    "near_miss": [("worker", MUST_HAVE["near_miss"][1], set())],
    "quality_defect": [("worker", MUST_HAVE["quality_defect"][1], set())],
}


def hungarian(cost: list[list[float]]) -> list[int]:
    """Kuhn-Munkres for an n x m cost matrix with n <= m: returns the column chosen for each row (min total cost)."""
    n, m = len(cost), len(cost[0]) if cost else 0
    if n == 0:
        return []
    if n > m:
        raise ValueError("more slots than candidates")
    INF = float("inf")
    u, v, p, way = [0.0] * (n + 1), [0.0] * (m + 1), [0] * (m + 1), [0] * (m + 1)
    for i in range(1, n + 1):
        p[0], j0 = i, 0
        minv, used = [INF] * (m + 1), [False] * (m + 1)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], INF, 0
            for j in range(1, m + 1):
                if not used[j]:
                    cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j], way[j] = cur, j0
                    if minv[j] < delta:
                        delta, j1 = minv[j], j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break
    out = [0] * n
    for j in range(1, m + 1):
        if p[j]:
            out[p[j] - 1] = j - 1
    return out


def _travel(sim, agent_id: str, x: int, y: int) -> float:
    a = sim.agents.get(agent_id)
    if a is None:
        return BIG
    return 2.0 * (abs(a.pos[0] - x) + abs(a.pos[1] - y))  # seconds at 1 m/s on a 2 m grid (Manhattan lower bound)


def slot_cost(slot, cand, incident: Incident, plant, sim) -> float:
    kind_, skills, certs = slot
    if cand.type != kind_:
        return BIG
    agent = sim.agents.get(cand.id)
    busy = 60.0 if agent is not None and agent.status == "moving" else 0.0
    if kind_ == "agv":
        h = cand.health or {}
        if h.get("service_overdue") and incident.severity in ("high", "critical"):
            return BIG
        return _travel(sim, cand.id, incident.x, incident.y) + 3.0 * (h.get("faults_90d") or 0) \
            + (100 - (h.get("battery_health_pct") or 100)) + busy
    w = plant.entity(cand.id)
    have = set(cand.skills)
    if skills and not have & skills:
        return BIG
    if any(not plant.cert_valid(w, c) for c in certs):
        return BIG
    if incident.kind == "part_shortage" and "tugger_driving" in have and not plant.cert_valid(w, "forklift"):
        return BIG  # R-TRAIN: an untrained tugger driver may not drive
    return _travel(sim, cand.id, incident.x, incident.y) + busy


def assign_plan(incident: Incident, cands: dict, plant, sim, clauses: list[dict] | None = None,
                exclude: set[str] | None = None) -> tuple[Plan | None, dict]:
    """-> (plan or None when no feasible assignment exists, details for the trace)."""
    exclude = exclude or set()
    slots = SLOTS.get(incident.kind) or [("worker", WORKER_NEEDS.get(incident.kind, (set(), None))[0], set())]
    pool = [c for c in cands["workers"] + cands["agvs"] if c.id not in exclude]
    need_agv = any(s[0] == "agv" for s in slots)
    if need_agv and not any(c.type == "agv" for c in pool):
        slots = [s for s in slots if s[0] != "agv"]  # no AGV available: the worker brings the parts
    if not pool or len(slots) > len(pool):
        return None, {"reason": "not enough candidates for the required roles", "slots": len(slots), "pool": len(pool)}
    cost = [[slot_cost(s, c, incident, plant, sim) for c in pool] for s in slots]
    pick = hungarian(cost)
    chosen = [(slots[i], pool[j], cost[i][j]) for i, j in enumerate(pick)]
    if any(c >= BIG for _, _, c in chosen):
        missing = [s[0] + (f" with {sorted(s[1])}" if s[1] else "") for s, _, c in chosen if c >= BIG]
        return None, {"reason": f"no feasible candidate for {missing}", "cost_matrix": cost}
    workers = [c.id for s, c, _ in chosen if c.type == "worker"]
    agvs = [c.id for s, c, _ in chosen if c.type == "agv"]
    routes = [{"agv_id": a, "to_station": incident.station_id, "speed_mps": 1.0, "crossing_speed_mps": 0.5}
              for a in agvs if incident.station_id]
    total = round(sum(c for _, _, c in chosen), 1)
    plan = Plan(incident_id=incident.id, assignee_ids=workers + agvs, agv_routes=routes,
                priority=SEVERITY_PRIORITY.get(incident.severity, "high"),
                rationale=f"Optimal assignment (Hungarian) over {len(pool)} candidates for {len(slots)} role(s); "
                          f"total cost {total} (travel s + reliability + load); AGVs 0.5 m/s at crossings.",
                citations=[c["ref"] for c in (clauses or [])[:2]])
    return plan, {"cost": total, "slots": [s[0] for s in slots],
                  "chosen": {c.id: round(cost_, 1) for _, c, cost_ in chosen}}


def plan_score(plan: Plan, dryrun: dict) -> float:
    """Lower is better: slowest arrival plus a small cost per person and robot committed."""
    etas = [e for e in (dryrun.get("eta_s") or {}).values() if e is not None]
    return round((max(etas) if etas else 999.0) + 15.0 * len(plan.assignee_ids), 1)
