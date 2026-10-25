"""The standard trace envelope: the fields the hackathon's trace format names, on every JSONL step record.

Each step record keeps its full `detail` and also gets trace_id, agent_name, action, input_summary, output_summary,
from_agent, target_agent, confidence, retry_count and status, so a reader can follow the hand-offs between agents
without knowing each agent's detail keys. target_agent is the agent that ran next for the same incident; it is filled
in when the run ends (`fill_targets`), and the last step of a run hands to "response".
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ACTIONS = {
    "reflex": "fence_and_stop", "monitor": "classify_alarm", "memory": "recall_lessons",
    "investigator": "query_history", "candidates": "rank_candidates", "playbook": "reuse_playbook",
    "verify_plan": "send_inspector", "planner": "write_plan", "critic": "check_safety", "dryrun": "simulate_plan",
    "refine": "compare_plans", "restraint": "decide", "gate": "human_gate", "executor": "dispatch",
    "verifier": "verify_outcome", "arbiter": "arbitrate_resources", "router": "choose_next_agent",
    "safe_mode": "fallback_plan", "intake": "classify_message", "coordinator": "split_question",
    "sql": "query_zetaris", "logs": "search_traces", "floor": "read_world_model", "analyst": "connect_findings",
    "evidence_critic": "validate_claims", "critic_llm": "validate_claims", "result": "answer", "lab": "lab_stage",
}


def _short(x: Any) -> str:
    return str(x).split(":")[-1]


def _ids(xs: Any, n: int = 4) -> str:
    xs = [_short(x) for x in (xs or [])]
    return ", ".join(xs[:n]) + (f" +{len(xs) - n}" if len(xs) > n else "") if xs else "none"


def _n(x: Any) -> int:
    return x if isinstance(x, int) else len(x or [])


def _clip(s: Any, n: int = 200) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _eta(x: Any) -> str:
    if isinstance(x, dict):
        return ", ".join(f"{_short(k)} {v:g} s" if isinstance(v, (int, float)) else f"{_short(k)} {v}" for k, v in x.items())
    return f"{x} s"


def _output(agent: str, ok: bool, d: dict) -> str:
    if d.get("error"):
        return f"error: {d['error']}"
    if agent == "reflex":
        return (f"fenced {d.get('fence_cells', 0)} cells; stopped {_ids(d.get('stopped'))}; "
                f"rerouted {_ids(d.get('rerouted'))}")
    if agent == "candidates":
        return f"workers {_ids(d.get('workers'))}; AGVs {_ids(d.get('agvs'))}"
    if agent in ("planner", "playbook", "verify_plan"):
        routes = [f"{_short(r.get('agv_id'))}->{r.get('to_station')}" for r in d.get("agv_routes") or []
                  if isinstance(r, dict)]
        v = f"v{d['version']} " if d.get("version") else ""
        return (f"{v}plan: send {_ids(d.get('assignees'))}; AGVs {', '.join(routes) or 'none'}; "
                f"cites {len(d.get('citations') or [])} clause(s)")
    if agent == "critic" and "kept" in d:  # Ask the floor's evidence critic
        return f"kept {_n(d.get('kept'))} claim(s), dropped {_n(d.get('dropped'))} unbacked"
    if agent == "critic":
        rules = [v.get("rule_id") for v in d.get("violations") or [] if isinstance(v, dict)]
        v = f"v{d['version']} " if d.get("version") else ""
        return (f"{v}rejected: {', '.join(rules)}; the plan goes back with the clauses" if rules
                else f"{v}approved: no rule broken")
    if agent == "dryrun":
        return (f"passed, ETA {_eta(d.get('eta_s'))}" if ok else f"failed: {'; '.join(map(str, d.get('reasons') or []))}")
    if agent == "restraint":
        return f"{d.get('decision')} (score {d.get('score')})"
    if agent == "gate":
        return f"{d.get('gate')}: {'; '.join(map(str, d.get('reasons') or []))}"
    if agent == "executor":
        return f"dispatched, ETA {_eta(d.get('eta_s'))}" if ok else f"blocked: {d.get('blocked')}"
    if agent == "verifier":
        mm = d.get("mismatches")
        return f"mismatch, re-plan: {mm}" if mm else "the floor changed as predicted"
    if agent == "memory":
        return f"{len(d.get('lessons') or [])} lesson(s); playbook {d.get('playbook') or 'none'}"
    if agent == "refine":
        return f"kept {d.get('chosen')}: {d.get('reason')}"
    if agent == "router":
        return f"{d.get('note') or d.get('reason') or ''} (rejected {d.get('rejected')})" if d.get("rejected") \
            else str(d.get("note") or d.get("reason") or "")
    if agent == "arbiter":
        return str(d.get("note") or "")
    if agent == "coordinator":
        return f"{len(d.get('tasks') or [])} sub-task(s)"
    if agent == "sql":
        return f"{d.get('rows')} row(s)" if ok else f"refused or failed: {d.get('error')}"
    if agent == "intake":
        return f"{d.get('intent')}: {d.get('understood_as')}"
    if agent == "analyst":
        return str(d.get("summary") or f"{len(d.get('findings') or [])} finding(s)")
    if agent == "lab":
        if d.get("effect_pct") is not None:
            return f"effect {d['effect_pct']}% (95% CI {d.get('ci_low_pct')}% to {d.get('ci_high_pct')}%)"
        return str(d.get("hypothesis") or d.get("rule") or d.get("policy") or "")
    for k in ("summary", "verdict", "note", "decision"):
        if d.get(k):
            return str(d[k])
    return "ok" if ok else "failed"


def _status(agent: str, ok: bool, d: dict) -> str:
    if agent == "critic" and "kept" in d:
        return "partial_success" if _n(d.get("dropped")) and _n(d.get("kept")) else ("success" if ok else "failed")
    if agent in ("critic", "dryrun") and not ok:
        return "needs_revision"
    if agent == "verifier" and d.get("mismatches"):
        return "replan"
    if agent == "router" and (d.get("error") or d.get("rejected") or d.get("fallback")):
        return "fallback"
    if agent == "safe_mode":
        return "fallback"
    if agent == "restraint" and d.get("decision") == "ESCALATE":
        return "escalated"
    if agent == "gate" and d.get("gate") == "awaiting_approval":
        return "awaiting_human"
    if agent == "sql" and not ok and d.get("attempt", 1) < 2:
        return "retrying"
    return "success" if ok else "failed"


def _confidence(agent: str, d: dict) -> float | None:
    for k in ("p_real", "confidence", "score", "success_rate"):
        v = d.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return round(float(v), 3)
    return None


def envelope(run_id: Any, agent: str, ok: bool, detail: dict, subject: str = "", prev: str | None = None) -> dict:
    """The standard fields for one step; `subject` is what the run is about (an incident, a question)."""
    d = detail or {}
    inp = subject
    if agent in ("planner",) and isinstance(d.get("prompt"), dict) and d["prompt"].get("critic_feedback"):
        inp += f"; critic feedback {', '.join(map(str, d['prompt']['critic_feedback']))}"
    if d.get("plan_source"):
        inp += f"; checks the {d['plan_source']} plan"
    if agent == "sql" and d.get("purpose"):
        inp = str(d["purpose"])
    attempt = d.get("attempt")
    return {"trace_id": f"trace-{run_id}", "agent_name": agent, "action": ACTIONS.get(agent, agent),
            "input_summary": _clip(inp), "output_summary": _clip(_output(agent, ok, d)),
            "from_agent": prev, "target_agent": None, "confidence": _confidence(agent, d),
            "retry_count": max(int(attempt) - 1, 0) if isinstance(attempt, (int, float)) else 0,
            "status": _status(agent, ok, d)}


def fill_targets(path: Path, key: str = "incident") -> None:
    """Set target_agent on the records of the run that just ended: the next agent for the same incident, and
    "response" for its last step. Records that already have a target (earlier runs) are left as they are."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    recs = []
    for line in lines:
        try:
            recs.append(json.loads(line))
        except ValueError:
            recs.append(line)
    last: dict = {}
    for i, r in enumerate(recs):
        if isinstance(r, dict) and "agent_name" in r and r.get("target_agent") is None:
            k = r.get(key)
            if k in last:
                recs[last[k]]["target_agent"] = r["agent_name"]
            last[k] = i
    for i in last.values():
        recs[i]["target_agent"] = "response"
    path.write_text("".join((json.dumps(r, default=str) if isinstance(r, dict) else r) + "\n" for r in recs),
                    encoding="utf-8")
