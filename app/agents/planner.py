"""Planner: NEMOTRON_DEEP chooses among the candidates and returns a strict Plan (chat_json via app/llm.py).

Every id in the plan must exist in the world model (World Model `ground`) and must have been offered as a
candidate. The critic's violations come back as structured feedback for the next attempt.
"""

from __future__ import annotations

import json
from typing import Callable

from app.contracts import Incident, Plan, Violation
from app.engines.shopfloor_world import ground

LAST_MODEL: dict[str, str | None] = {}  # which model answered the last planner call (for the trace)
LAST_CALL: dict[str, dict] = {}  # requested/answering model, latency and Nightshift receipt of the last call
PLANNER_TIMEOUT_S = 90  # the deep lane reasons before answering; 20 s is too short

SYSTEM = (
    "You are the dispatch planner for a car final-assembly hall. Choose assignees ONLY from the candidate ids "
    "given (exact ids). For part shortages and AGV faults, send one kitting AGV from the AGV candidates to the "
    "station with an agv_route (to_station is a station id such as F30). AGVs normally drive at 1.0 m/s. "
    "Priority is one of low, medium, high, critical. Keep the rationale under 60 words. "
    "Put the refs of the relevant_clauses your plan follows in `citations` (exact ref strings)."
)


def make_plan(incident: Incident, cands: dict, llm_json: Callable, feedback: list[Violation] | None = None,
              clauses: list[dict] | None = None) -> Plan:
    payload = {
        "incident": incident.model_dump(exclude={"truth_false_alarm"} | ({"note"} if incident.note is None else set())),
        "worker_candidates": [c.model_dump() for c in cands["workers"]],
        "agv_candidates": [c.model_dump() for c in cands["agvs"]],
    }
    if clauses:
        payload["relevant_clauses"] = [{"ref": c["ref"], "text": c["text"]} for c in clauses]
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": json.dumps(payload)}]
    if feedback:
        messages.append({"role": "user", "content": "The safety critic rejected your previous plan. Fix every "
                         "violation and return a corrected plan: "
                         + json.dumps([v.model_dump(exclude={"citations"}) | {"clauses": [c.ref for c in v.citations]}
                                       for v in feedback])})
    reply = llm_json("deep", messages, Plan, timeout_s=PLANNER_TIMEOUT_S, role="planner")
    plan = reply.data if hasattr(reply, "data") else reply
    LAST_MODEL["planner"] = getattr(reply, "model", None)
    from app.models import call_info
    LAST_CALL["planner"] = call_info(reply)
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


def rule_based_plan(messages: list[dict]) -> Plan:
    """Deterministic planner used in SAMPLE_MODE when no recorded reply exists for this input. It reads the
    same payload the model gets and still goes through the critic, dry-run and restraint gate."""
    payload = json.loads(messages[1]["content"])
    inc = payload["incident"]
    workers = [c["id"] for c in payload["worker_candidates"][:1]]
    if len(messages) > 2:  # critic feedback: drop the people it named, add someone with the skills it asked for
        feedback = json.loads(messages[-1]["content"].split(": ", 1)[1])
        named = {v["detail"].split(" ")[0] for v in feedback}
        pool = [c for c in payload["worker_candidates"] if c["id"] not in named]
        workers = [c["id"] for c in pool[:1]]
        for v in feedback:
            if "skills [" in v["detail"]:
                need = set(json.loads(v["detail"].split("skills ", 1)[1].replace("'", '"')))
                fit = next((c["id"] for c in pool if need & set(c["skills"])), None)
                if fit and fit not in workers:
                    workers.append(fit)
    agvs = [c["id"] for c in payload["agv_candidates"][:1]]
    routes = [{"agv_id": agvs[0], "to_station": inc["station_id"], "speed_mps": 1.0, "crossing_speed_mps": 0.5}] \
        if agvs and inc.get("station_id") else []
    cites = [c["ref"] for c in payload.get("relevant_clauses", [])[:2]]
    return Plan(incident_id=inc["id"], assignee_ids=workers or agvs or ["none"], agv_routes=routes, priority="high",
                rationale="Sample-mode rule-based plan: nearest qualified candidates; AGV slows to 0.5 m/s at crossings.",
                citations=cites)


def fallback_plan(incident: Incident, cands: dict) -> Plan:
    """Rule-based safe-mode plan when Nightshift is unavailable: best candidates, safe speeds."""
    assignees = [c.id for c in cands["workers"][:1]] or [c.id for c in cands["agvs"][:1]]
    routes = [{"agv_id": cands["agvs"][0].id, "to_station": incident.station_id, "speed_mps": 1.0,
               "crossing_speed_mps": 0.5}] if cands["agvs"] and incident.station_id else []
    return Plan(incident_id=incident.id, assignee_ids=assignees or ["none"], agv_routes=routes, priority="high",
                rationale="Safe-mode fallback: nearest qualified candidates, crossing speed 0.5 m/s; escalated to a human.")
