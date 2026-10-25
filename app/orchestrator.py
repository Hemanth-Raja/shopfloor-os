"""The agent loop for one incident.

    reflex -> monitor -> candidates -> planner <-> critic (up to 3 attempts) -> dry-run -> restraint
           -> executor -> verifier (re-plan on mismatch)

Every step appends one JSONL line to logs/trace.jsonl, with timings. If app.llm raises LLMUnavailable, the
loop switches to safe mode: a rule-based fallback plan and an ESCALATE decision.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from app.tracefmt import envelope, fill_targets
from app.agents.candidates import find_candidates
from app.agents.dryrun import dry_run
from app.agents.executor import execute, verify
from app.agents.monitor import assess
from app.agents.planner import LAST_CALL, LAST_MODEL, fallback_plan, grounding_violations, make_plan
from app.agents.reflex import reflex
from app.agents.restraint import decide
from app.agents.safety_critic import review
from app.contracts import Incident, RestraintDecision, StepResult, Trace
from app.engines.citations import KIND_RULES
from app.llm import LLMUnavailable

TRACE_PATH = Path(__file__).resolve().parents[1] / "logs" / "trace.jsonl"
AUTONOMY = ("auto", "supervised", "advisory")
SUPPORTED_KINDS = {"part_shortage", "agv_fault", "overheat", "spill",  # kinds with a planning playbook
                   "robot_cell_fault", "torque_fault", "near_miss", "quality_defect"}
# a person walks into the hazard (spill clean-up, machine repair, entering a stopped robot cell)
PERSON_HAZARD_KINDS = {"spill", "overheat", "robot_cell_fault"}
SIGN_OFF_KINDS = {"near_miss"}  # safety events: a supervisor signs off whatever the autonomy level


def executable_plan(trace: Trace):
    """The plan as it is dispatched: VERIFY sends only the first assignee to check, with no AGV moves."""
    if trace.plan is None or trace.decision is None:
        return None
    if trace.decision.decision == "VERIFY":
        return trace.plan.model_copy(update={"agv_routes": [], "assignee_ids": trace.plan.assignee_ids[:1]})
    return trace.plan


def gate_reasons(trace: Trace, plant, autonomy: str) -> list[str]:
    """Why this decision needs a human before anything moves (empty list: it may auto-execute)."""
    d = trace.decision.decision
    reasons = []
    if autonomy == "advisory":
        reasons.append("advisory mode: the agents only recommend; a person dispatches")
    elif autonomy == "supervised":
        reasons.append("supervised mode: every dispatch waits for approval")
    if d in ("ESCALATE", "SAFE_HOLD", "DO_NOTHING"):
        reasons.append(f"{d}: the agents are not confident enough to act alone")
    if trace.incident.kind in PERSON_HAZARD_KINDS and d in ("ACT", "VERIFY"):
        reasons.append(f"a person would enter the {trace.incident.kind.replace('_', ' ')} hazard area")
    if trace.safe_mode and d in ("ACT", "VERIFY"):
        reasons.append("safe mode: Nightshift is unavailable, so this is the rules plan; a person approves it")
    if trace.incident.kind in SIGN_OFF_KINDS and d in ("ACT", "VERIFY"):
        reasons.append("a near miss is a safety event: a supervisor signs off before the AGV is released")
    for wid in (trace.plan.assignee_ids if trace.plan else []):
        w = plant.entity(wid)
        if w and w["type"] == "worker":
            lapsed = [c for c in w["attrs"].get("cert_expiry", {}) if not plant.cert_valid(w, c)]
            if lapsed:
                reasons.append(f"{wid.split(':')[-1]} has a lapsed certificate ({', '.join(lapsed)})")
    return reasons
MAX_ATTEMPTS = 3
VERIFY_TICKS = 6


class Orchestrator:
    def __init__(self, plant, sim, llm_json: Callable, llm_text: Callable | None = None,
                 trace_path: Path | None = TRACE_PATH, memory=None, on_step: Callable | None = None,
                 citations=None, run_id: str | None = None, echo: bool = False, autonomy: str = "auto"):
        self.plant, self.sim = plant, sim
        self.llm_json, self.llm_text = llm_json, llm_text
        self.run_id = run_id
        if run_id and trace_path == TRACE_PATH:
            trace_path = TRACE_PATH.parent / f"trace-{run_id}.jsonl"  # one JSONL file per run
        self.trace_path = trace_path
        self.echo = echo  # also print one [INFO] line per step to stdout
        if autonomy not in AUTONOMY:
            raise ValueError(f"autonomy must be one of {AUTONOMY}")
        self.autonomy = autonomy  # auto: routine ACT runs itself; supervised: always ask; advisory: recommend only
        self.lessons: list = []
        self.memory = memory  # optional PlaybookStore: lessons from past incidents seed the planner
        self.on_step = on_step  # optional callback(incident_id, StepResult), e.g. the web server's event bus
        if citations is None:
            from app.engines.citations import default_index
            citations = default_index()
        self.citations = citations  # OSHA clauses + plant SOPs indexed in H-MEM
        self._prev: dict[str, str] = {}  # last agent per incident, for the trace's from_agent

    def finish_trace(self, incident_id: str) -> None:
        """Fill target_agent in this run's trace file (per-run files only; the shared legacy file is left alone)."""
        if self.trace_path and self.trace_path != TRACE_PATH:
            fill_targets(self.trace_path)
        self._prev.pop(incident_id, None)

    def _step(self, trace: Trace, name: str, started: float, ok: bool, **detail) -> None:
        result = StepResult(step=name, ok=ok, ms=round((time.perf_counter() - started) * 1000, 2), detail=detail)
        trace.steps.append(result)
        if self.on_step:
            self.on_step(trace.incident.id, result)
        inc = trace.incident
        prev = self._prev.get(inc.id)
        env = envelope(self.run_id or inc.id, name, ok, detail,
                       subject=f"{inc.kind} at {inc.station_id or inc.zone_id}", prev=prev)
        self._prev[inc.id] = name
        if self.trace_path:
            self.trace_path.parent.mkdir(parents=True, exist_ok=True)
            with self.trace_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts": datetime.now().astimezone().isoformat(), "run_id": self.run_id,
                                     "incident": inc.id, "agent": name, "tick": self.sim.tick,
                                     **env, **result.model_dump()}, default=str) + "\n")
        if self.echo:
            extra = {k: detail[k] for k in ("model", "attempt", "decision", "playbook") if detail.get(k) is not None}
            if name == "critic" and detail.get("violations"):
                extra["violations"] = [v["rule_id"] for v in detail["violations"]]
            print(f"[INFO] run_id={self.run_id} agent={name} action={env['action']} status={env['status']} "
                  f"from={prev} ms={result.ms} | {env['output_summary']} {json.dumps(extra, default=str)}", flush=True)

    def _plan_loop(self, trace: Trace, incident: Incident, cands: dict):
        feedback = list(self.lessons) or None  # critic findings carry over to a re-plan
        for attempt in range(1, MAX_ATTEMPTS + 1):
            t = time.perf_counter()
            clauses = self.citations.for_incident(incident.kind, f"{incident.kind} at {incident.station_id} {incident.zone_id}")
            plan = make_plan(incident, cands, self.llm_json, feedback, clauses)
            cited = self.citations.resolve(plan.citations)
            prompt = {"incident": f"{incident.kind} at {incident.station_id or incident.zone_id}",
                      "worker_candidates": [c.id.split(":")[-1] for c in cands["workers"]],
                      "agv_candidates": [c.id.split(":")[-1] for c in cands["agvs"]],
                      "clauses_shown": [c["ref"] for c in clauses],
                      "critic_feedback": [v.rule_id for v in (feedback or [])]}
            self._step(trace, "planner", t, True, attempt=attempt, model=LAST_MODEL.get("planner"),
                       model_call=LAST_CALL.get("planner"), prompt=prompt,
                       assignees=plan.assignee_ids,
                       agv_routes=[r.model_dump() for r in plan.agv_routes], priority=plan.priority,
                       citations=cited, uncited=[r for r in plan.citations if r not in self.citations.by_ref])
            t = time.perf_counter()
            violations = grounding_violations(plan, cands, self.plant.world) + review(
                plan, incident, self.plant, self.sim, self.citations).violations
            governing = [] if violations else [c for rid in KIND_RULES.get(incident.kind, [])
                                               for c in self.citations.for_rule(rid, top_n=1)]
            self._step(trace, "critic", t, not violations, attempt=attempt,
                       violations=[v.model_dump() for v in violations], citations=governing)
            if not violations:
                return plan
            trace.critic_rejections += 1
            feedback = violations
            for v in violations:
                if v.rule_id not in {x.rule_id for x in self.lessons} and v.rule_id != "GROUNDING":
                    self.lessons.append(v)
        return None

    def handle(self, incident: Incident, stall: tuple[int, int] | None = None) -> Trace:
        trace = self._handle(incident, stall)
        self.finish_trace(incident.id)
        if self.memory is not None:
            self.memory.record(trace)
        return trace

    def _handle(self, incident: Incident, stall: tuple[int, int] | None = None) -> Trace:
        """Run the loop. `stall=(route_index, after_ticks)` stalls that planned AGV mid-execution (demo/test)."""
        trace = Trace(incident=incident, data_version=getattr(self.plant, "version", None))
        t = time.perf_counter()
        reflex_out = reflex(self.sim, incident, self.plant)
        self._step(trace, "reflex", t, True, **reflex_out)

        t = time.perf_counter()
        mon = assess(incident, self.llm_text)
        self._step(trace, "monitor", t, True, **mon)

        if incident.kind not in SUPPORTED_KINDS:  # no playbook, candidates or critic rules: a person takes over
            trace.decision = RestraintDecision(decision="ESCALATE", score=0.0, reasons=[
                f"no response playbook for '{incident.kind}' yet: the area is fenced and a supervisor must take over"])
            self._step(trace, "restraint", time.perf_counter(), True, **trace.decision.model_dump())
            return trace

        if self.memory is not None:
            t = time.perf_counter()
            book = self.memory.recall(incident.kind, incident.station_id)
            self.lessons = self.memory.lessons_as_feedback(book)
            self._step(trace, "memory", t, True, playbook=book["key"] if book else None,
                       lessons=[v.rule_id for v in self.lessons],
                       success_rate=book["success_rate"] if book else None)

        excluded: set[str] = set()
        for round_ in range(1, MAX_ATTEMPTS + 1):
            t = time.perf_counter()
            cands = find_candidates(self.plant, self.sim, incident, exclude=excluded)
            self._step(trace, "candidates", t, bool(cands["workers"] or cands["agvs"]),
                       workers=[c.id for c in cands["workers"]], agvs=[c.id for c in cands["agvs"]])
            try:
                plan = self._plan_loop(trace, incident, cands)
            except LLMUnavailable as err:
                trace.safe_mode = True
                plan = fallback_plan(incident, cands)
                trace.plan = plan
                self._step(trace, "safe_mode", time.perf_counter(), False, error=str(err)[:200])
                trace.decision = RestraintDecision(decision="ESCALATE", score=0.0,
                                                   reasons=["Nightshift unavailable: safe mode, rule-based fallback plan"])
                self._step(trace, "restraint", time.perf_counter(), True, **trace.decision.model_dump())
                return trace
            trace.plan = plan
            dr = None
            if plan is not None:
                t = time.perf_counter()
                dr = dry_run(plan, incident, self.plant, self.sim)
                self._step(trace, "dryrun", t, dr["ok"], **{k: v for k, v in dr.items() if k != "ok"})
            t = time.perf_counter()
            trace.decision = decide(incident, mon, dr, plan is not None, self.plant.stats)
            self._step(trace, "restraint", t, True, **trace.decision.model_dump())
            if plan is None:
                return trace
            t = time.perf_counter()
            trace.autonomy = self.autonomy
            reasons = gate_reasons(trace, self.plant, self.autonomy)
            if reasons:
                trace.gate = "advisory" if self.autonomy == "advisory" else "awaiting_approval"
                trace.gate_reasons = reasons
                self._step(trace, "gate", t, True, gate=trace.gate, reasons=reasons, autonomy=self.autonomy)
                return trace  # nothing moves until a person approves
            trace.gate, trace.gate_reasons = "auto_executed", ["routine ACT: critic and dry-run passed; you can override"]
            self._step(trace, "gate", t, True, gate=trace.gate, reasons=trace.gate_reasons, autonomy=self.autonomy)
            plan = executable_plan(trace)
            t = time.perf_counter()
            for aid in list(plan.assignee_ids) + [r.agv_id for r in plan.agv_routes]:
                if aid in self.sim.agents and aid not in trace.dispatch:
                    trace.dispatch[aid] = list(self.sim.agents[aid].pos)
            expected = execute(plan, incident, self.plant, self.sim)
            self._step(trace, "executor", t, all(e["path"] for e in expected.values()),
                       eta_s={a: e["eta_s"] for a, e in expected.items()})
            if stall and round_ == 1 and plan.agv_routes:
                route = plan.agv_routes[min(stall[0], len(plan.agv_routes) - 1)]
                self.sim.inject_stall(route.agv_id, self.sim.tick + stall[1])
            self.sim.run(VERIFY_TICKS)
            t = time.perf_counter()
            mismatches = verify(self.sim, expected)
            self._step(trace, "verifier", t, not mismatches, mismatches=mismatches)
            if not mismatches:
                return trace
            trace.replans += 1
            for m in mismatches:
                excluded.add(m["agent"])
                if self.sim.agents[m["agent"]].status == "stalled":
                    self.sim.stop(m["agent"], status="fault")
        trace.decision = RestraintDecision(decision="ESCALATE", score=0.0, reasons=["verifier mismatches persisted"])
        return trace
