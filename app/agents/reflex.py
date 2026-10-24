"""Reflex layer: rules only, no model calls, well under 100 ms.

It geofences the incident area at once, stops AGVs inside the fence, and re-routes AGVs whose remaining
path crosses it. If no route avoids the fence, the AGV is stopped.
"""

from __future__ import annotations

import time

from app.contracts import Incident

FENCE_RADIUS = {"spill": 2, "overheat": 1, "agv_fault": 1, "part_shortage": 0,
                "robot_cell_fault": 2, "torque_fault": 1, "near_miss": 2, "quality_defect": 0}


LOTO_KINDS = {"robot_cell_fault", "overheat"}  # only people with valid lockout/tagout may stay inside


def reflex(sim, incident: Incident, plant=None) -> dict:
    started = time.perf_counter()
    radius = FENCE_RADIUS.get(incident.kind, 1)  # unknown kinds: a small safety fence
    fence = sim.grid.cells_within(incident.x, incident.y, radius) if radius else set()
    sim.set_geofence(fence, key=incident.id, kind=incident.kind)
    stopped, rerouted = [], []
    for agent in sorted(sim.agents.values(), key=lambda a: a.id):
        if agent.kind != "agv" or agent.status in ("fault", "held"):
            continue
        if agent.pos in fence:
            sim.stop(agent.id)
            stopped.append(agent.id)
            continue
        remaining = agent.path[max(0, sim.tick - agent.path_start):] if agent.status == "moving" else []
        if any(c in fence for c in remaining):
            if sim.assign(agent.id, agent.goal, agent.speed_mps, agent.crossing_speed_mps) is None:
                sim.stop(agent.id)
                stopped.append(agent.id)
            else:
                rerouted.append(agent.id)
    evacuated = []
    if incident.kind in LOTO_KINDS and plant is not None and fence:
        taken = {a.pos for a in sim.agents.values()} | fence
        for agent in sorted(sim.agents.values(), key=lambda a: a.id):
            heading_in = agent.status == "moving" and agent.goal in fence
            if agent.kind != "worker" or (agent.pos not in fence and not heading_in):
                continue
            w = plant.entity(agent.id)
            if w and plant.cert_valid(w, "lockout_tagout"):
                continue  # an authorised technician may stay
            # inside: step out to the nearest free cell; still outside but heading in: stop where they are
            out = agent.pos if agent.pos not in fence else sim._free_cell_near(agent.pos[0], agent.pos[1], taken)
            if sim.assign(agent.id, out) is not None:
                taken.add(out)
                evacuated.append(agent.id)
    return {"fence_cells": len(fence), "stopped": stopped, "rerouted": rerouted, "evacuated": evacuated,
            "ms": round((time.perf_counter() - started) * 1000, 2)}
