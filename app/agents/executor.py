"""Executor and verifier: apply a plan to the sim, then check that the world changed as predicted."""

from __future__ import annotations

from app.contracts import Incident, Plan
from app.sim.sim import TICK_S

STALL_TICKS = 3
DRIFT_CELLS = 2
SMKT_PICKUP = (7, 13)  # supermarket exit: every kit run picks parts here first


def agv_legs(route, plant, sim) -> tuple[tuple[int, int], tuple[int, int]]:
    """(pickup cell, line-side delivery cell south of the conveyor) for an AGV route."""
    x, y = plant.station_xy(route.to_station)
    return sim.grid.nearest_walkable(*SMKT_PICKUP), sim.grid.nearest_walkable(x, y + 2)


def targets(plan: Plan, incident: Incident, plant, sim) -> dict:
    """agent id -> (goal cell, speed m/s, crossing speed m/s or None, pickup cell or None)."""
    out = {}
    for route in plan.agv_routes:
        pickup, delivery = agv_legs(route, plant, sim)
        out[route.agv_id] = (delivery, route.speed_mps, route.crossing_speed_mps, pickup)
    for aid in plan.assignee_ids:
        if aid in out or aid not in sim.agents:
            continue
        out[aid] = ((incident.x, incident.y), 1.0, None, None)
    return out


def execute(plan: Plan, incident: Incident, plant, sim, fleet=None, tokens: dict | None = None,
            key: str | None = None) -> dict:
    """Assign every agent in the plan; returns the expected paths (None means unreachable). With a `fleet`
    (app/connectors.py) each command carries the agent's lease token and an idempotency key."""
    expected = {}
    for aid, (goal, speed, crossing, via) in sorted(targets(plan, incident, plant, sim).items()):
        if fleet is not None:
            path = fleet.dispatch(aid, goal, speed, crossing, via, token=(tokens or {}).get(aid),
                                  key=f"{key}:{aid}" if key else None)
        else:
            path = sim.assign(aid, goal, speed, crossing, via=via)
        expected[aid] = {"path": path, "start": sim.tick,
                         "eta_s": (len(path) - 1) * TICK_S if path else None}
    return expected


def verify(sim, expected: dict) -> list[dict]:
    """Mismatches between where agents should be and where they are (e.g. a stalled AGV)."""
    out = []
    for aid, exp in sorted(expected.items()):
        agent = sim.agents[aid]
        if exp["path"] is None:
            out.append({"agent": aid, "issue": "unreachable"})
            continue
        i = min(sim.tick - exp["start"], len(exp["path"]) - 1)
        want = exp["path"][i]
        drift = abs(want[0] - agent.pos[0]) + abs(want[1] - agent.pos[1])
        stuck = len(sim.history) >= STALL_TICKS and agent.pos != exp["path"][-1] and all(
            h.get(aid) == agent.pos for h in sim.history[-STALL_TICKS:])
        if agent.status == "stalled" or stuck or drift > DRIFT_CELLS:
            out.append({"agent": aid, "issue": "stalled" if agent.status == "stalled" or stuck else "drift",
                        "expected": list(want), "actual": list(agent.pos), "drift_cells": drift})
    return out
