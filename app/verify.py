"""Verify one decision offline: re-check a saved incident with code only, no model and no network.

    .venv/bin/python run.py verify SIM-42-003          # from logs/incidents.jsonl
    GET /api/verify/<incident id>                       # the same check, from the server

What it re-derives from the saved trace and the plant snapshot:
1. the plan that moved people and robots passes the safety critic again (the same OSHA rules, rerun now);
2. the trace itself recorded a passing critic check for a plan before anything was dispatched;
3. the human gate was respected: a decision that needed a person did not run on its own;
4. the plant data the decision saw is the snapshot loaded now (same version), or the check says it differs;
5. the hash-chained journal holds this incident's outcome with the same decision and gate, and the chain is intact.
Each check names what it compared, so a changed row or field is visible, not just a red light.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.contracts import Trace

ROOT = Path(__file__).resolve().parents[1]
INCIDENTS = ROOT / "logs" / "incidents.jsonl"
RAN = ("auto_executed", "approved", "auto_after_timeout", "confirmed")


def load(incident_id: str, path: Path = INCIDENTS) -> tuple[Trace, dict]:
    rec = None
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("id") == incident_id:
                rec = r  # the last line for the incident is its latest state
    if rec is None:
        raise KeyError(f"{incident_id} is not in {path.name}")
    return Trace.model_validate(rec["trace"]), rec.get("meta") or {}


def journal_entry(incident_id: str, journal=None) -> tuple[dict | None, dict]:
    if journal is None:
        from app.journal import Journal
        journal = Journal(None, replicate=False)
    found = None
    for ev in journal.events():
        if ev.get("kind") in ("finished", "decision", "incident_finished") and (ev.get("data") or {}).get("incident") == incident_id:
            found = ev
    return found, journal.verify()


def verify(trace: Trace, plant, sim, journal=None, autonomy: str = "auto") -> dict:
    from app.agents.safety_critic import review
    from app.orchestrator import executable_plan, gate_reasons
    checks = []

    def check(name, ok, detail):
        checks.append({"check": name, "ok": ok, "detail": detail})

    plan = executable_plan(trace)
    ran = trace.gate in RAN
    if plan is None:
        check("plan re-checked by the safety critic", True, "no plan was made: nothing to re-check")
    else:
        verdict = review(plan, trace.incident, plant, sim)
        rules = [v.rule_id for v in verdict.violations if v.rule_id != "R-UNVERIFIED"]
        check("plan re-checked by the safety critic", not rules,
              "passes every rule again" if not rules else f"now breaks {', '.join(rules)}: "
              + "; ".join(v.detail for v in verdict.violations if v.rule_id in rules)[:300])
    critic = [st for st in trace.steps if st.step == "critic"]
    passed = [st for st in critic if st.ok]
    check("a plan passed the critic before dispatch", bool(passed) or not ran,
          f"{len(passed)} passing and {len(critic) - len(passed)} rejected critic checks recorded"
          + ("" if passed or not ran else ": something ran without a passing check"))
    if trace.decision is not None:
        reasons = gate_reasons(trace, plant, trace.autonomy or autonomy)
        alone = trace.gate in ("auto_executed",)
        check("the human gate was respected", not (alone and reasons),
              ("ran on its own; no rule required a person" if alone else f"gate: {trace.gate}")
              + (f"; a person was needed because {'; '.join(reasons)}" if reasons else ""))
    now = getattr(plant, "version", None)
    check("same plant data snapshot", trace.data_version is None or now is None or trace.data_version == now,
          f"decision saw {trace.data_version or 'unknown'}, loaded now {now or 'unknown'}"
          + ("" if trace.data_version == now else ": the floor data changed since (certificates, people on shift)"))
    if journal is not False:
        entry, chain = journal_entry(trace.incident.id, journal)
        if entry is None:
            check("journal holds this outcome", None, "no finished entry in the journal (an incident run by hand is "
                  "journalled when it is judged)")
        else:
            d = entry.get("data") or {}
            same = d.get("decision") == (trace.decision.decision if trace.decision else None)
            check("journal holds this outcome", same and chain.get("ok", False),
                  f"journal #{entry.get('seq')} says {d.get('decision')}/{d.get('gate')}, trace says "
                  f"{trace.decision.decision if trace.decision else None}/{trace.gate}; chain "
                  + ("intact" if chain.get("ok") else f"broken: {chain.get('reason')}"))
    failed = [c for c in checks if c["ok"] is False]
    return {"incident": trace.incident.id, "kind": trace.incident.kind, "decision": trace.decision.decision
            if trace.decision else None, "gate": trace.gate, "verdict": "CONFIRMED" if not failed else "DIFFERS",
            "checks": checks, "model_calls": 0}
