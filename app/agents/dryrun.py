"""Lab Level 1 dry-run: simulate the plan for 60 s headless on a copy of the sim. No model calls."""

from __future__ import annotations

from app.agents.executor import execute
from app.contracts import Incident, Plan
from app.sim.sim import TICK_S

HORIZON_S = 60
ETA_LIMIT_S = {"critical": 180, "high": 300, "medium": 600, "low": 900}


def dry_run(plan: Plan, incident: Incident, plant, sim) -> dict:
    trial = sim.clone()
    expected = execute(plan, incident, plant, trial)
    reasons = []
    unreachable = [a for a, e in expected.items() if e["path"] is None]
    if unreachable:
        reasons.append(f"unreachable: {unreachable}")
    trial.run(int(HORIZON_S / TICK_S))
    if trial.collisions:
        reasons.append(f"{len(trial.collisions)} collisions, first {trial.collisions[0]}")
    limit = ETA_LIMIT_S[plan.priority]
    etas = {a: e["eta_s"] for a, e in expected.items()}
    late = {a: t for a, t in etas.items() if t is not None and t > limit}
    if late:
        reasons.append(f"ETA over {limit}s: {late}")
    return {"ok": not reasons, "reasons": reasons, "eta_s": etas, "horizon_s": HORIZON_S}
