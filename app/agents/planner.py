"""Planner: NEMOTRON_DEEP chooses among the candidates and returns a strict Plan (chat_json via app/llm.py).

Every id in the plan must exist in the world model (World Model `ground`) and must have been offered as a
candidate. The critic's violations come back as structured feedback for the next attempt.
"""

from __future__ import annotations

import json
from typing import Callable

from app.contracts import Incident, Plan, Violation
from app.engines.shopfloor_world import ground

PLANNER_TIMEOUT_S = 120  # the deep lane reasons before answering; 20 s is too short

SYSTEM = (
    "You are the dispatch planner for a car final-assembly hall. Choose assignees ONLY from the candidate ids "
    "given (exact ids). For part shortages and AGV faults, send one kitting AGV from the AGV candidates to the "
    "station with an agv_route (to_station is a station id such as F30). AGVs normally drive at 1.0 m/s. "
    "Priority is one of low, medium, high, critical. Keep the rationale under 60 words."
)


def make_plan(incident: Incident, cands: dict, llm_json: Callable, feedback: list[Violation] | None = None) -> Plan:
    payload = {
        "incident": incident.model_dump(exclude={"truth_false_alarm"}),
        "worker_candidates": [c.model_dump() for c in cands["workers"]],
        "agv_candidates": [c.model_dump() for c in cands["agvs"]],
    }
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": json.dumps(payload)}]
    if feedback:
        messages.append({"role": "user", "content": "The safety critic rejected your previous plan. Fix every "
                         "violation and return a corrected plan: " + json.dumps([v.model_dump() for v in feedback])})
    reply = llm_json("deep", messages, Plan, timeout_s=PLANNER_TIMEOUT_S)
    plan = reply.data if hasattr(reply, "data") else reply
    return plan.model_copy(update={"incident_id": incident.id})


def grounding_violations(plan: Plan, cands: dict, world) -> list[Violation]:
    """Ids must exist in the world model and must be among the offered candidates."""
    offered = {c.id for c in cands["workers"] + cands["agvs"]}
    ids = list(plan.assignee_ids) + [r.agv_id for r in plan.agv_routes]
    out = [Violation(rule_id="GROUNDING", cfr_ref="World Model", detail=f"{i} is not in the world model")
           for i in ground(world, ids)]
    out += [Violation(rule_id="GROUNDING", cfr_ref="Candidates", detail=f"{i} was not offered as a candidate")
            for i in ids if i not in offered and not any(v.detail.startswith(i) for v in out)]
    return out


def fallback_plan(incident: Incident, cands: dict) -> Plan:
    """Rule-based safe-mode plan when Nightshift is unavailable: best candidates, safe speeds."""
    assignees = [c.id for c in cands["workers"][:1]] or [c.id for c in cands["agvs"][:1]]
    routes = [{"agv_id": cands["agvs"][0].id, "to_station": incident.station_id, "speed_mps": 1.0,
               "crossing_speed_mps": 0.5}] if cands["agvs"] and incident.station_id else []
    return Plan(incident_id=incident.id, assignee_ids=assignees or ["none"], agv_routes=routes, priority="high",
                rationale="Safe-mode fallback: nearest qualified candidates, crossing speed 0.5 m/s; escalated to a human.")
