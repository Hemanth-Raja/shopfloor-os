"""Reflex layer: rules only, no model calls, well under 100 ms.

It geofences the incident area at once, stops AGVs inside the fence, and re-routes AGVs whose remaining
path crosses it. If no route avoids the fence, the AGV is stopped.
"""

from __future__ import annotations

import time

from app.contracts import Incident

FENCE_RADIUS = {"spill": 2, "overheat": 1, "agv_fault": 1, "part_shortage": 0}


def reflex(sim, incident: Incident) -> dict:
    started = time.perf_counter()
    radius = FENCE_RADIUS[incident.kind]
    fence = sim.grid.cells_within(incident.x, incident.y, radius) if radius else set()
    sim.set_geofence(fence)
    stopped, rerouted = [], []
    for agent in sorted(sim.agents.values(), key=lambda a: a.id):
        if agent.kind != "agv" or agent.status == "fault":
            continue
        if agent.pos in fence:
            sim.stop(agent.id)
            stopped.append(agent.id)
            continue
        remaining = agent.path[max(0, sim.tick - agent.path_start):] if agent.status == "moving" else []
        if any(c in fence for c in remaining):
            if sim.assign(agent.id, agent.goal, agent.speed_mps) is None:
                sim.stop(agent.id)
                stopped.append(agent.id)
            else:
                rerouted.append(agent.id)
    return {"fence_cells": len(fence), "stopped": stopped, "rerouted": rerouted,
            "ms": round((time.perf_counter() - started) * 1000, 2)}
