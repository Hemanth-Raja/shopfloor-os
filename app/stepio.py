"""What each agent step took in: rebuilt from the incident and the outputs of the steps before it. No model calls.

The trace records every step's output (`detail`). An agent's input is the incident plus the earlier outputs it
reads, so the Incidents history can show input and output side by side for every hop.
"""

from __future__ import annotations

import re

_ID = re.compile(r"^(worker|agv|station|machine):[\w:-]+$")


def _short(x):
    if isinstance(x, str):
        return x.split(":")[-1] if _ID.match(x) else x
    if isinstance(x, list):
        return [_short(v) for v in x]
    if isinstance(x, dict):
        return {_short(k): _short(v) for k, v in x.items()}
    return x


def _plan(d: dict) -> dict:
    out = {"assignees": d.get("assignees")}
    if d.get("agv_routes"):
        out["agv_routes"] = [{k: r.get(k) for k in ("agv_id", "to_station", "speed_mps", "crossing_speed_mps")
                              if r.get(k) is not None} for r in d["agv_routes"]]
    return out


def step_inputs(trace) -> list[dict]:
    """One input dict per step in trace.steps, in order."""
    inc = trace.incident
    where = inc.station_id or inc.zone_id
    base = {"incident": f"{inc.kind} at {where}", "severity": inc.severity}
    last: dict[str, dict] = {}  # agent -> its latest output
    plan: dict | None = None  # the latest plan any planner produced
    plan_from = None
    out = []
    for st in trace.steps:
        d, k = st.detail or {}, st.step
        g = lambda a, key, default=None: (last.get(a) or {}).get(key, default)  # noqa: E731
        if k == "reflex":
            i = {**base, "position": f"station {where}"}
        elif k == "monitor":
            i = {**base, "reported note": getattr(inc, "note", None) or "-", "sensors": getattr(inc, "sensors", None) or "-",
                 "reads": "thresholds per machine and shift"}
        elif k == "memory":
            i = {"kind": inc.kind, "station": where, "alarm real?": g("monitor", "verdict")}
        elif k == "investigator":
            i = {"kind": inc.kind, "station": where, "question": "how often was this kind real here, downtime, cost",
                 "source": "Zetaris (sf_ops.incidents), read-only"}
        elif k == "candidates":
            i = {**base, "alarm real?": g("monitor", "verdict"), "playbook": g("memory", "playbook"),
                 "reads": "World Model: certified people on shift, healthy AGVs, leases held by other incidents"}
        elif k == "planner":
            i = {"people": g("candidates", "workers"), "agvs": g("candidates", "agvs"), "lessons": g("memory", "lessons"),
                 "attempt": d.get("attempt")}
            if (d.get("attempt") or 1) > 1 and last.get("critic"):
                i["critic feedback"] = [v.get("rule_id") for v in g("critic", "violations", [])]
        elif k == "playbook":
            i = {"playbook": g("memory", "playbook"), "success rate": g("memory", "success_rate"),
                 "people": g("candidates", "workers"), "agvs": g("candidates", "agvs")}
        elif k == "verify_plan":
            i = {"alarm real?": g("monitor", "verdict"), "p_real": g("monitor", "p_real"), "people": g("candidates", "workers")}
        elif k in ("critic", "dryrun"):
            i = {"plan": plan, "plan from": d.get("plan_source") or plan_from}
            if k == "critic" and d.get("version") == 2:  # the anytime planner's v2, checked before refine decides
                i = {"plan": "Nemotron's v2 plan (shown under refine)", "plan from": d.get("plan_source")}
        elif k == "refine":
            i = {"v1 (rules)": plan, "v2 critic violations": [v.get("rule_id") for v in g("critic_v2", "violations", [])]}
        elif k == "restraint":
            i = {"p_real": g("monitor", "p_real"), "sensors agree": g("monitor", "sensor_agreement"),
                 "critic ok": not g("critic", "violations", []) if "critic" in last else None,
                 "dry run ok": not g("dryrun", "reasons", []) if "dryrun" in last else None,
                 "false-alarm share": g("investigator", "false_alarm_share"), "plan": plan}
        elif k == "gate":
            i = {"decision": g("restraint", "decision"), "autonomy": d.get("autonomy"), "plan": plan}
        elif k == "executor":
            i = {"plan": plan}
        elif k == "verifier":
            i = {"expected arrival (s)": g("executor", "eta_s") or g("dryrun", "eta_s")}
        elif k == "router":
            i = {"after": out and trace.steps[len(out) - 1].step, "state": "what has run, what is legal next"}
        else:
            prev = trace.steps[len(out) - 1].step if out else None
            i = {**base, "after": prev}
        out.append({kk: _short(v) for kk, v in i.items() if v is not None})
        last["critic_v2" if k == "critic" and d.get("version") == 2 else k] = d  # v2's check is not the chosen plan's
        if k in ("planner", "playbook", "verify_plan") and d.get("assignees") is not None:
            plan, plan_from = _plan(d), {"planner": f"planner v{d.get('version') or 1}"}.get(k, k)
    return out
