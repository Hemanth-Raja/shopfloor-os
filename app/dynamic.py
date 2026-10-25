"""Dynamic orchestrator: an orchestrator agent chooses which agent runs next, hop by hop.

    reflex (always first, in code) -> router -> monitor -> router -> investigator | memory | candidates ...
        -> playbook | verify_plan | planner -> critic -> dryrun -> restraint -> gate -> executor -> verifier

At each decision point the router (NEMOTRON_FAST via Nightshift, app/llm.py) sees a compact state and the agents
that are legal right now, and returns strict JSON {next_agent, reason, args}. Code, not the model, decides what is
legal. These guardrails cannot be skipped:
- the reflex always runs first;
- the executor runs only after the latest plan passed the critic and the dry run, and the human gate allowed it;
- the human gate always runs before anything moves, per the autonomy level;
- an illegal or unknown choice is rejected, logged as a 'router' step, and the rule-based router decides instead;
- once Nightshift is unavailable, the planner is replaced by safe mode (rule-based plan, ESCALATE).
A forced move (one legal agent) costs no model call. The rule-based router is used in SAMPLE_MODE, when
Nightshift is down, and after MAX_ROUTER_CALLS. A hop budget ends runaway loops with ESCALATE.

The linear pipeline (app/orchestrator.Orchestrator) is kept for comparison: run.py incident --linear.
"""

from __future__ import annotations

import contextlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field

from app.agents.candidates import find_candidates
from app.agents.dryrun import dry_run
from app.agents.executor import execute, verify
from app.agents.monitor import assess
from app.agents.assign import assign_plan, plan_score
from app.agents.planner import LAST_CALL, LAST_MODEL, fallback_plan, grounding_violations, make_plan, make_plan_info
from app.agents.restraint import STAT_KIND, decide
from app.agents.safety_critic import review
from app.contracts import Incident, Plan, RestraintDecision, Trace, Violation
from app.engines.citations import KIND_RULES
from app import orchestrator as linear
from app.llm import LLMUnavailable
from app.models import BudgetExhausted
from app.orchestrator import MAX_ATTEMPTS, VERIFY_TICKS, Orchestrator, executable_plan, gate_reasons

HOP_BUDGET = 24
MAX_ROUTER_CALLS = 6
ROUTER_TIMEOUT_S = 45

REGISTRY = {
    "monitor": "classify the alarm: real or sensor noise (thresholds, Bosch vibration evidence)",
    "memory": "recall H-MEM lessons and the playbook for this incident kind and station",
    "investigator": "look up history through Zetaris (guarded SQL) and our own traces for this kind and station",
    "candidates": "rank qualified people and healthy AGVs from the World Model",
    "playbook": "build the plan from a proven H-MEM playbook (no model call)",
    "verify_plan": "the alarm is in doubt: send one qualified person to check first, no AGVs (no model call)",
    "planner": "anytime planner: an optimal-assignment plan at once (v1); Nemotron's plan (v2) in the background",
    "refine": "check Nemotron's plan (v2) with the critic and the dry run; swap it in only if it is safe and better",
    "critic": "deterministic OSHA/SOP rule check of the latest plan",
    "dryrun": "60 s headless simulation of the latest plan",
    "restraint": "ACT, VERIFY, SAFE_HOLD, ESCALATE or DO_NOTHING from confidence, sensors and cost",
    "gate": "human gate for the autonomy level",
    "executor": "dispatch the people and AGVs",
    "verifier": "check the world changed as predicted",
    "safe_mode": "Nightshift is down: rule-based plan and ESCALATE",
}

ROUTER_SYSTEM = (
    "You are the orchestrator of a team of agents handling an incident in a car final-assembly hall. Choose the next "
    "agent: next_agent must be exactly one of the agent names listed in `choose_next_agent_from`. Take the shortest safe path: use a proven playbook or a one-person check instead of the "
    "planner when that is enough; look up history first when the alarm is ambiguous or the sensors disagree; after a "
    "critic rejection, re-plan with the feedback or re-pick candidates. Safety steps (critic, dry run, human gate) are "
    "enforced in code and cannot be skipped. Reply with JSON {next_agent, reason, args}; reason under 25 words; "
    "args may be {}."
)


class RouteChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    next_agent: str = Field(min_length=3, max_length=24)
    reason: str = Field(min_length=3, max_length=300)
    args: dict[str, Any] = Field(default_factory=dict)


@dataclass
class State:
    mon: dict | None = None
    memory_done: bool = False
    book: dict | None = None
    history: dict | None = None
    cands: dict | None = None
    cands_stale: bool = False
    plan: Plan | None = None
    plan_source: str | None = None
    critic_ok: bool | None = None
    attempts: int = 0
    feedback: list | None = None
    dryrun: dict | None = None
    gated: bool = False
    executed: bool = False
    expected: dict | None = None
    verified: bool | None = None
    round: int = 1
    excluded: set = field(default_factory=set)
    done: bool = False
    nightshift_down: bool = False
    budget_paced: bool = False  # the call budget shed the planner's model call: rules plan, not safe mode
    pending: object = None  # anytime planning: the model's plan (v2), being computed in the background


def plan_resources(plan: Plan | None) -> list[str]:
    if plan is None:
        return []
    return sorted({a for a in plan.assignee_ids if ":" in a} | {r.agv_id for r in plan.agv_routes})


def incident_history(run_sql: Callable, incident: Incident) -> dict:
    """The investigator in incident mode: fixed, guarded SQL over sf_ops.incidents (no model call)."""
    from app.investigate.sqlguard import check
    kind = STAT_KIND.get(incident.kind, incident.kind)
    where = f"kind = '{kind}'" + (f" AND station_id = '{incident.station_id}'" if incident.station_id else "")
    sql = check("SELECT count(*) AS n, sum(CASE WHEN false_alarm THEN 1 ELSE 0 END) AS false_alarms, "
                "round(avg(downtime_min), 1) AS avg_downtime_min, round(avg(cost_usd), 0) AS avg_cost_usd "
                f"FROM sf_ops.incidents WHERE {where}")
    row = (run_sql(sql) or [[0, 0, None, None]])[0]
    n, fa = int(float(row[0] or 0)), int(float(row[1] or 0))
    out = {"sql": " ".join(sql.split()), "incidents": n, "false_alarms": fa,
           "false_alarm_share": round(fa / n, 2) if n else None,
           "avg_downtime_min": row[2], "avg_cost_usd": row[3]}
    agv = (incident.sensors or {}).get("agv") or (incident.agv_id or "").split(":")[-1] or None
    if agv:
        sql2 = check("SELECT shift_code, count(*) AS faults FROM sf_ops.incidents "
                     f"WHERE agv_id = '{agv}' GROUP BY shift_code ORDER BY faults DESC")
        out["agv"] = {"agv_id": agv, "sql": " ".join(sql2.split()),
                      "by_shift": {r[0]: int(float(r[1])) for r in run_sql(sql2)}}
    where_txt = f"{incident.kind.replace('_', ' ')} at {incident.station_id or incident.zone_id}"
    out["summary"] = (f"{n} past {where_txt}" + (f", {fa} false alarms ({out['false_alarm_share']:.0%})" if n else "")
                      + (f", avg {row[2]} min downtime" if n else ""))
    return out


class DynamicOrchestrator(Orchestrator):
    def __init__(self, *args, router_llm: Callable | None = None, run_sql: Callable | None = None,
                 leases=None, risk: float = 0.0, sim_lock=None, anytime: bool = True, fleet=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.leases = leases  # shift mode: a LeaseBook shared by every concurrent incident
        self.risk = risk  # this incident's priority (app/autonomy/dispatcher.incident_risk)
        self.sim_lock = sim_lock  # shift mode: hops that touch the floor run under the floor's lock
        self.fleet = fleet  # shift mode: FleetManager connector (fencing tokens, idempotency keys)
        self.anytime = anytime  # v1 by optimal assignment at once, the model's v2 swapped in only if safe and better
        self.router_llm = router_llm  # None: rule-based router only (SAMPLE_MODE, tests)
        self.run_sql = run_sql  # for the investigator; None: it is not offered
        self._hop: int | None = None
        self._route: dict | None = None
        self.router_calls = 0
        self.last_router_call: dict | None = None

    # every agent step carries the hop number and why the router chose it
    def _step(self, trace: Trace, name: str, started: float, ok: bool, **detail) -> None:
        if self._hop is not None and name != "router":
            detail = {**detail, "hop": self._hop, "route": self._route}
        super()._step(trace, name, started, ok, **detail)

    # ---------------------------------------------------------------- legality (guardrails, in code)

    def playbook_usable(self, s: State, incident: Incident) -> bool:
        b = s.book
        return bool(b and b["key"] == f"{incident.kind}@{incident.station_id or '*'}" and b.get("successes", 0) >= 1
                    and b.get("success_rate", 0) >= 0.75 and b.get("working_pattern"))

    def legal(self, s: State, trace: Trace) -> list[str]:
        if s.done:
            return []
        if s.mon is None:
            return ["monitor"]
        if trace.decision is not None:
            if s.plan is None:
                return []
            if not s.gated:
                return ["gate"]
            if trace.gate != "auto_executed":
                return []  # waiting for a person, or advisory: nothing moves
            if not s.executed:
                return ["executor"]
            return ["verifier"] if s.verified is None else []
        if s.plan is not None:
            if s.critic_ok is None:
                return ["critic"]
            if s.critic_ok is False:
                if any(v.rule_id == "R-UNVERIFIED" for v in (s.feedback or [])):
                    return ["restraint"]  # default-deny: re-planning cannot fix an uncheckable rule
                if s.pending is not None:
                    return ["refine"]  # the model's plan may fix it
                return (["planner", "candidates"] if s.attempts < MAX_ATTEMPTS else ["restraint"])
            if s.dryrun is None:
                return ["dryrun"]
            return ["refine"] if s.pending is not None else ["restraint"]
        opts = []
        if self.memory is not None and not s.memory_done:
            opts.append("memory")
        if s.history is None and self.run_sql is not None:
            opts.append("investigator")
        if s.cands is None or s.cands_stale:
            opts.append("candidates")
        if s.cands is not None:
            if self.playbook_usable(s, trace.incident):
                opts.append("playbook")
            if s.cands["workers"] and (s.mon["p_real"] < 0.8 or not s.mon["sensor_agreement"]):
                opts.append("verify_plan")  # a one-person check only makes sense while the alarm is in doubt
            opts.append("planner")
        if s.nightshift_down and "planner" in opts and not self.anytime:
            opts[opts.index("planner")] = "safe_mode"  # health failure forces safe mode
        return opts

    # ---------------------------------------------------------------- routing

    def rule_route(self, s: State, legal: list[str], incident: Incident) -> tuple[str, str]:
        if len(legal) == 1:
            return legal[0], "the only legal next step"
        mon = s.mon
        noisy = mon["p_real"] < 0.5 and not mon["sensor_agreement"]
        ambiguous = noisy or not mon["sensor_agreement"] or mon["p_real"] < 0.8
        if "memory" in legal:
            return "memory", "check what we learned from past incidents first"
        if "investigator" in legal and ambiguous:
            return "investigator", "the alarm is ambiguous: look at the history before planning"
        if "candidates" in legal and (s.cands is None or s.cands_stale):
            return "candidates", ("re-pick people and robots without the ones that failed" if s.cands_stale
                                  else "find qualified people and healthy robots")
        if "verify_plan" in legal and noisy:
            return "verify_plan", "sensors disagree: one person checks first, no planner needed"
        if "playbook" in legal:
            return "playbook", f"a proven playbook exists ({s.book['successes']}/{s.book['outcomes']} successes): skip the planner"
        if "planner" in legal:
            return "planner", ("fix the critic's violations" if s.critic_ok is False else
                               "no playbook fits: optimal assignment now, Nemotron refines in the background")
        if "safe_mode" in legal:
            return "safe_mode", "Nightshift is unavailable"
        return legal[0], "default order"

    def compact(self, s: State, trace: Trace, legal: list[str]) -> dict:
        inc = trace.incident
        return {
            "incident": {"kind": inc.kind, "station": inc.station_id, "zone": inc.zone_id, "severity": inc.severity,
                         "note": inc.note, "sensors": inc.sensors},
            "monitor": s.mon and {k: s.mon[k] for k in ("verdict", "p_real", "sensor_agreement")},
            "memory": None if not s.memory_done else (s.book and {"key": s.book["key"], "successes": s.book["successes"],
                                                                  "outcomes": s.book["outcomes"],
                                                                  "lessons": [l["rule_id"] for l in s.book["lessons"]],
                                                                  "proven": self.playbook_usable(s, inc)}),
            "history": s.history and s.history.get("summary"),
            "candidates": s.cands and {"workers": [c.id.split(":")[-1] for c in s.cands["workers"]],
                                       "agvs": [c.id.split(":")[-1] for c in s.cands["agvs"]],
                                       "stale": s.cands_stale},
            "plan": s.plan and {"source": s.plan_source, "assignees": [a.split(":")[-1] for a in s.plan.assignee_ids],
                                "agv_routes": len(s.plan.agv_routes)},
            "critic": None if s.critic_ok is None else ("passed" if s.critic_ok else
                                                        [v.rule_id for v in (s.feedback or [])]),
            "planner_attempts": s.attempts, "round": s.round,
            "path_so_far": [p["agent"] for p in trace.path],
            "choose_next_agent_from": [{"name": name, "does": REGISTRY[name]} for name in legal],
        }

    def route(self, s: State, trace: Trace, legal: list[str]) -> tuple[str, str, str, dict]:
        """-> (agent, by, reason, args). Model choices outside `legal` are rejected and logged."""
        if len(legal) == 1:
            return legal[0], "forced", "the only legal next step", {}
        if self.router_llm is not None and not s.nightshift_down and self.router_calls < MAX_ROUTER_CALLS:
            self.router_calls += 1
            t = time.perf_counter()
            try:
                reply = self.router_llm("fast", [{"role": "system", "content": ROUTER_SYSTEM},
                                                 {"role": "user", "content": json.dumps(self.compact(s, trace, legal),
                                                                                        default=str)}],
                                        RouteChoice, timeout_s=ROUTER_TIMEOUT_S, role="router",
                                        deadline=self.deadline_at)
                choice = reply.data if hasattr(reply, "data") else reply
                from app.models import call_info
                self.last_router_call = call_info(reply)
                if choice.next_agent in legal:
                    return choice.next_agent, f"nemotron ({getattr(reply, 'model', '?')})", choice.reason, choice.args
                agent, why = self.rule_route(s, legal, trace.incident)
                self._step(trace, "router", t, False, rejected=choice.next_agent, reason=choice.reason, legal=legal,
                           fallback=agent, note="not a legal move: rejected by the guardrails")
                return agent, "rules (model choice rejected)", why, {}
            except BudgetExhausted as err:  # pacing, not an outage: this hop by rules, the model again next time
                self._step(trace, "router", t, True, note="model budget pacing: rule-based router for this hop",
                           error=str(err)[:200])
            except LLMUnavailable as err:
                s.nightshift_down = True
                self._step(trace, "router", t, False, error=str(err)[:200], note="Nightshift unavailable: rule-based router")
            except Exception as err:  # noqa: BLE001 - a malformed reply: fall back for this hop
                self._step(trace, "router", t, False, error=f"{type(err).__name__}: {str(err)[:160]}",
                           note="unusable router reply: rule-based router for this hop")
            legal = self.legal(s, trace)  # health may have changed what is legal
        agent, why = self.rule_route(s, legal, trace.incident)
        return agent, "rules", why, {}

    # ---------------------------------------------------------------- the loop

    # no floor access while they wait on a model: may run while others move. refine waits for Nemotron's v2 (up to
    # anytime_wait_s) outside the floor lock and takes the lock only to check v2 (a live run showed Approve and the
    # floor clock freezing while refine held the lock during the model wait)
    UNLOCKED = {"monitor", "memory", "investigator", "planner", "refine"}

    def _floor(self, agent: str):
        return self.sim_lock if self.sim_lock is not None and agent not in self.UNLOCKED else contextlib.nullcontext()

    def _handle(self, incident: Incident, stall: tuple[int, int] | None = None) -> Trace:
        steps = self.run_steps(incident, stall)
        while True:
            try:
                next(steps)
            except StopIteration as done:
                return done.value

    def run_steps(self, incident: Incident, stall: tuple[int, int] | None = None):
        """The orchestrator as a generator: one yield per hop, so a dispatcher can interleave many incidents.
        Returns the Trace (StopIteration.value)."""
        trace = yield from self._run_steps(incident, stall)
        self.finish_trace(incident.id)
        return trace

    def _run_steps(self, incident: Incident, stall: tuple[int, int] | None = None):
        trace = Trace(incident=incident, mode="dynamic", data_version=getattr(self.plant, "version", None))
        self.trace = trace
        from app.models import CFG
        self.deadline_at = time.monotonic() + CFG["defaults"].get("incident_budget_s", 240)  # passed to every model call
        s = State()
        self.state = s
        t = time.perf_counter()
        from app.agents.reflex import reflex
        self._hop, self._route = 0, {"by": "code", "reason": "the reflex always runs first"}
        trace.path.append({"hop": 0, "agent": "reflex", **self._route})
        with self._floor("reflex"):
            fenced = reflex(self.sim, incident, self.plant)
        self._step(trace, "reflex", t, True, **fenced)
        yield "reflex"
        if incident.kind not in linear.SUPPORTED_KINDS:  # no playbook, candidates or critic rules: a person takes over
            trace.decision = RestraintDecision(decision="ESCALATE", score=0.0, reasons=[
                f"no response playbook for '{incident.kind}' yet: the area is fenced and a supervisor must take over"])
            self._hop, self._route = 1, {"by": "code", "reason": "unsupported incident kind"}
            trace.path.append({"hop": 1, "agent": "restraint", **self._route})
            self._step(trace, "restraint", time.perf_counter(), True, **trace.decision.model_dump())
            self._hop = self._route = None
            return trace
        hop = 0
        while True:
            self._check_preempted(s, trace)
            legal = self.legal(s, trace)
            if not legal:
                break
            hop += 1
            if hop > HOP_BUDGET:
                trace.decision = RestraintDecision(decision="ESCALATE", score=0.0,
                                                   reasons=[f"hop budget of {HOP_BUDGET} used up: a person takes over"])
                self._hop, self._route = hop, {"by": "code", "reason": "hop budget exhausted"}
                self._step(trace, "restraint", time.perf_counter(), True, **trace.decision.model_dump())
                break
            self.last_router_call = None
            agent, by, why, args = self.route(s, trace, legal)
            if agent not in self.legal(s, trace):  # belt and braces: never run an illegal agent
                agent, why = self.rule_route(s, self.legal(s, trace), incident)
                by = "rules"
            self._hop, self._route = hop, {"by": by, "reason": why,
                                           **({"model_call": self.last_router_call} if self.last_router_call else {})}
            trace.path.append({"hop": hop, "agent": agent, "by": by, "reason": why})
            with self._floor(agent):
                getattr(self, f"_do_{agent}")(s, trace, incident, stall, args)
            yield agent
        self._hop = self._route = None
        return trace

    # ---------------------------------------------------------------- agents

    def _do_monitor(self, s, trace, incident, stall, args):
        t = time.perf_counter()
        s.mon = assess(incident, self.llm_text, deadline=getattr(self, "deadline_at", None))
        self._step(trace, "monitor", t, True, **s.mon)

    def _do_memory(self, s, trace, incident, stall, args):
        t = time.perf_counter()
        s.book = self.memory.recall(incident.kind, incident.station_id)
        self.lessons = self.memory.lessons_as_feedback(s.book)
        s.memory_done = True
        self._step(trace, "memory", t, True, playbook=s.book["key"] if s.book else None,
                   lessons=[v.rule_id for v in self.lessons],
                   success_rate=s.book["success_rate"] if s.book else None,
                   proven=self.playbook_usable(s, incident))

    def _do_investigator(self, s, trace, incident, stall, args):
        t = time.perf_counter()
        try:
            from app.cache import cache
            agv = (incident.sensors or {}).get("agv") or (incident.agv_id or "").split(":")[-1] or None
            key = (incident.kind, incident.station_id, agv, int(time.time() // 600))
            s.history = dict(cache("history", 300).get(key, lambda: incident_history(self.run_sql, incident)))
            ok = True
        except Exception as err:  # noqa: BLE001 - history is optional evidence
            s.history, ok = {"error": f"{type(err).__name__}: {str(err)[:160]}", "summary": "history unavailable"}, False
        self._step(trace, "investigator", t, ok, **s.history)

    def _do_candidates(self, s, trace, incident, stall, args):
        t = time.perf_counter()
        busy = self.leases.blocked_for(incident.id, self.risk) if self.leases else set()
        s.cands = find_candidates(self.plant, self.sim, incident, exclude=s.excluded | busy)
        s.cands_stale = False
        self._step(trace, "candidates", t, bool(s.cands["workers"] or s.cands["agvs"]),
                   workers=[c.id for c in s.cands["workers"]], agvs=[c.id for c in s.cands["agvs"]],
                   **({"held_by_other_incidents": sorted(busy)} if busy else {}))

    def _new_plan(self, s, plan: Plan, source: str) -> None:
        s.plan, s.plan_source, s.critic_ok, s.dryrun = plan, source, None, None
        s.attempts += 1

    def _do_planner(self, s, trace, incident, stall, args):
        if s.attempts == 0:
            s.feedback = list(self.lessons) or None  # lessons recalled from H-MEM seed the first attempt
        t = time.perf_counter()
        clauses = self.citations.for_incident(incident.kind, f"{incident.kind} at {incident.station_id} {incident.zone_id}")
        if self.anytime and (s.attempts == 0 or s.nightshift_down or s.budget_paced):
            return self._anytime_plan(s, trace, incident, clauses, t)
        try:
            plan = make_plan(incident, s.cands, self.llm_json, s.feedback, clauses, deadline=getattr(self, "deadline_at", None))
        except BudgetExhausted as err:  # pacing, not an outage: the rules plan goes through the same critic and gate
            s.budget_paced = True
            self._step(trace, "planner", t, True, attempt=s.attempts + 1, note="model budget pacing: rules plan",
                       error=str(err)[:200])
            return self._anytime_plan(s, trace, incident, clauses, t)
        except LLMUnavailable as err:
            s.nightshift_down = True
            self._step(trace, "planner", t, False, attempt=s.attempts + 1, error=str(err)[:200])
            if self.anytime:  # no model: the rules plan without the people the critic named
                return self._anytime_plan(s, trace, incident, clauses, t)
            return self._do_safe_mode(s, trace, incident, stall, {"error": str(err)})
        cited = self.citations.resolve(plan.citations)
        prompt = {"incident": f"{incident.kind} at {incident.station_id or incident.zone_id}",
                  "worker_candidates": [c.id.split(":")[-1] for c in s.cands["workers"]],
                  "agv_candidates": [c.id.split(":")[-1] for c in s.cands["agvs"]],
                  "clauses_shown": [c["ref"] for c in clauses],
                  "critic_feedback": [v.rule_id for v in (s.feedback or [])]}
        self._new_plan(s, plan, "planner")
        self._step(trace, "planner", t, True, attempt=s.attempts, model=LAST_MODEL.get("planner"),
                   model_call=LAST_CALL.get("planner"), prompt=prompt,
                   assignees=plan.assignee_ids, agv_routes=[r.model_dump() for r in plan.agv_routes],
                   priority=plan.priority, citations=cited,
                   uncited=[r for r in plan.citations if r not in self.citations.by_ref])

    # ---------------------------------------------------------------- anytime planning

    def _anytime_plan(self, s, trace, incident, clauses, t) -> None:
        """v1 at once by optimal assignment; the model's v2 starts in the background (unless Nightshift is down)."""
        named = {v.detail.split(" ")[0] for v in (s.feedback or []) if ":" in v.detail.split(" ")[0]}
        v1, info = assign_plan(incident, s.cands, self.plant, self.sim, clauses, exclude=s.excluded | named)
        if not (s.nightshift_down or s.budget_paced) and self.llm_json is not None and s.pending is None and s.attempts == 0:
            import concurrent.futures as cf
            pool = cf.ThreadPoolExecutor(max_workers=1)
            s.pending = pool.submit(make_plan_info, incident, s.cands, self.llm_json, s.feedback, clauses,
                                    getattr(self, "deadline_at", None))
            pool.shutdown(wait=False)
        if v1 is None:  # no feasible rules plan: wait for the model's plan instead
            self._step(trace, "planner", t, False, attempt=s.attempts + 1, version=1, model="rules (Hungarian)",
                       error=info.get("reason"), refining=s.pending is not None)
            if s.pending is not None:
                return self._take_v2_now(s, trace, incident)
            return self._do_safe_mode(s, trace, incident, None, {"error": f"no rules plan: {info.get('reason')}"})
        self._new_plan(s, v1, "rules v1")
        trace.safe_mode = trace.safe_mode or s.nightshift_down
        self._step(trace, "planner", t, True, attempt=s.attempts, version=1, model="rules (Hungarian)",
                   assignees=v1.assignee_ids, agv_routes=[r.model_dump() for r in v1.agv_routes], priority=v1.priority,
                   citations=self.citations.resolve(v1.citations), assignment=info,
                   refining=s.pending is not None, safe_mode=s.nightshift_down)

    def _check(self, plan, incident) -> list:
        violations = grounding_violations(plan, self.state.cands, self.plant.world) + review(
            plan, incident, self.plant, self.sim, self.citations).violations
        if self.leases:
            for lease in self.leases.conflicts(incident.id, self.risk, plan_resources(plan)):
                violations.append(Violation(rule_id="R-LEASE", cfr_ref="Resource arbitration",
                                            detail=f"{lease.resource} is committed to {lease.incident}"))
        return violations

    def _wait_v2(self, s):
        """-> (plan, model, call info) or (None, None, reason)."""
        from app.models import CFG
        fut, s.pending = s.pending, None
        wait = min(CFG["defaults"].get("anytime_wait_s", 90), max(0.0, getattr(self, "deadline_at", time.monotonic() + 90)
                                                                  - time.monotonic()))
        try:
            return fut.result(timeout=wait)
        except BudgetExhausted as err:  # pacing: keep the rules plan, which already passed the critic and the dry run
            return None, None, f"model budget pacing ({str(err)[:80]})"
        except LLMUnavailable as err:
            s.nightshift_down = True
            return None, None, f"Nightshift unavailable: {str(err)[:120]}"
        except Exception as err:  # noqa: BLE001 - timeout or an unusable reply: keep v1
            return None, None, f"{type(err).__name__}: {str(err)[:120] or 'not ready in time'}"

    def _take_v2_now(self, s, trace, incident) -> None:
        t = time.perf_counter()
        v2, model, info = self._wait_v2(s)
        if v2 is None:
            self._step(trace, "refine", t, False, chosen=None, reason=info)
            return self._do_safe_mode(s, trace, incident, None, {"error": info})
        self._new_plan(s, v2, "nemotron v2")
        self._step(trace, "refine", t, True, chosen="v2", reason="no rules plan was feasible", model=model,
                   model_call=info, v2={"assignees": v2.assignee_ids})

    def _do_refine(self, s, trace, incident, stall, args):
        t = time.perf_counter()
        v1, v1_ok = s.plan, bool(s.critic_ok and s.dryrun and s.dryrun["ok"])
        v2, model, info = self._wait_v2(s)
        with self._floor("critic"):
            return self._refine_checked(s, trace, incident, t, v1, v1_ok, v2, model, info)

    def _refine_checked(self, s, trace, incident, t, v1, v1_ok, v2, model, info):
        if v2 is None:
            trace.safe_mode = trace.safe_mode or s.nightshift_down
            self._step(trace, "refine", t, v1_ok, chosen="v1", reason=f"model plan unavailable ({info}): keep v1",
                       v1={"assignees": v1.assignee_ids})
            return
        cited = self.citations.resolve(v2.citations)
        violations = self._check(v2, incident)
        self._step(trace, "critic", t, not violations, attempt=s.attempts, version=2, plan_source="nemotron v2",
                   violations=[v.model_dump() for v in violations])
        dr2 = None
        if violations:
            trace.critic_rejections += 1
            for v in violations:  # the critic's findings still teach H-MEM
                if v.rule_id not in {x.rule_id for x in self.lessons} and v.rule_id not in ("GROUNDING", "R-LEASE"):
                    self.lessons.append(v)
        else:
            dr2 = dry_run(v2, incident, self.plant, self.sim)
            self._step(trace, "dryrun", time.perf_counter(), dr2["ok"], version=2,
                       **{k: v for k, v in dr2.items() if k != "ok"})
        v2_ok = bool(dr2 and dr2["ok"])
        s1 = plan_score(v1, s.dryrun) if v1_ok else None
        s2 = plan_score(v2, dr2) if v2_ok else None
        swap = v2_ok and (not v1_ok or s2 < s1)
        if swap:
            s.plan, s.plan_source, s.critic_ok, s.dryrun = v2, "nemotron v2", True, dr2
            trace.plan = v2
            reason = "v2 is safe and better" if v1_ok else "v1 failed; v2 is safe"
        elif v1_ok:
            reason = ("v2 broke " + ", ".join(sorted({v.rule_id for v in violations}))) if violations else \
                ("v2 failed the dry run" if not v2_ok else
                 f"same score ({s1}): keep v1, already checked" if s1 == s2 else f"v1 scores better ({s1} vs {s2})")
        else:  # neither is safe: back to the planner with the critic's feedback
            s.feedback = violations or s.feedback
            reason = "neither plan is safe: re-plan with the critic's feedback"
        self._step(trace, "refine", t, swap or v1_ok, chosen="v2" if swap else ("v1" if v1_ok else None), reason=reason,
                   model=model, model_call=info, citations=cited if swap else [],
                   v1={"assignees": v1.assignee_ids, "score": s1}, v2={"assignees": v2.assignee_ids, "score": s2,
                                                                         "violations": [v.rule_id for v in violations]})

    def _do_playbook(self, s, trace, incident, stall, args):
        """Re-use what worked: the playbook's pattern applied to today's best candidates. No model call."""
        t = time.perf_counter()
        pat, workers, agvs = s.book["working_pattern"], list(s.cands["workers"]), list(s.cands["agvs"])
        assignees = [workers[0].id] if "worker" in pat["assignee_types"] and workers else []
        routes = []
        for r in pat["agv_routes"]:
            if agvs:
                routes.append({**r, "agv_id": agvs.pop(0).id, "to_station": incident.station_id or r["to_station"]})
        assignees += [r["agv_id"] for r in routes if "agv" in pat["assignee_types"]]
        clauses = self.citations.for_incident(incident.kind, f"{incident.kind} at {incident.station_id} {incident.zone_id}")
        plan = Plan(incident_id=incident.id, assignee_ids=assignees or [c.id for c in s.cands["workers"][:1]],
                    agv_routes=routes, priority="high",
                    rationale=f"Proven H-MEM playbook {s.book['key']} ({s.book['successes']}/{s.book['outcomes']} "
                              f"successes); lessons applied: {[l['rule_id'] for l in s.book['lessons']] or 'none'}.",
                    citations=[c["ref"] for c in clauses[:2]])
        self._new_plan(s, plan, "playbook")
        self._step(trace, "playbook", t, True, playbook=s.book["key"], success_rate=s.book["success_rate"],
                   assignees=plan.assignee_ids, agv_routes=[r.model_dump() for r in plan.agv_routes],
                   citations=self.citations.resolve(plan.citations))

    def _do_verify_plan(self, s, trace, incident, stall, args):
        t = time.perf_counter()
        who = s.cands["workers"][0]
        clauses = self.citations.for_incident(incident.kind, f"{incident.kind} at {incident.station_id} {incident.zone_id}")
        plan = Plan(incident_id=incident.id, assignee_ids=[who.id], priority="medium",
                    rationale=f"Inspection first: {who.name} checks the {incident.kind.replace('_', ' ')} before "
                              "anything else moves.",
                    citations=[c["ref"] for c in clauses[:1]])
        self._new_plan(s, plan, "verify_plan")
        self._step(trace, "verify_plan", t, True, assignees=plan.assignee_ids,
                   citations=self.citations.resolve(plan.citations))

    def _do_critic(self, s, trace, incident, stall, args):
        t = time.perf_counter()
        violations = grounding_violations(s.plan, s.cands, self.plant.world) + review(
            s.plan, incident, self.plant, self.sim, self.citations).violations
        if self.leases:  # cross-incident check: nobody another, equally urgent incident is already using
            for lease in self.leases.conflicts(incident.id, self.risk, plan_resources(s.plan)):
                violations.append(Violation(rule_id="R-LEASE", cfr_ref="Resource arbitration",
                                            detail=f"{lease.resource} is committed to {lease.incident} "
                                                   f"(risk {lease.risk:g} >= {self.risk:g})"))
        governing = [] if violations else [c for rid in KIND_RULES.get(incident.kind, [])
                                           for c in self.citations.for_rule(rid, top_n=1)]
        self._step(trace, "critic", t, not violations, attempt=s.attempts, plan_source=s.plan_source,
                   violations=[v.model_dump() for v in violations], citations=governing)
        s.critic_ok = not violations
        if violations:
            trace.critic_rejections += 1
            s.feedback = violations
            for v in violations:
                if v.rule_id not in {x.rule_id for x in self.lessons} and v.rule_id != "GROUNDING":
                    self.lessons.append(v)
        else:
            trace.plan = s.plan

    def _do_dryrun(self, s, trace, incident, stall, args):
        t = time.perf_counter()
        s.dryrun = dry_run(s.plan, incident, self.plant, self.sim)
        self._step(trace, "dryrun", t, s.dryrun["ok"], **{k: v for k, v in s.dryrun.items() if k != "ok"})

    def _do_restraint(self, s, trace, incident, stall, args):
        t = time.perf_counter()
        plan_ok = bool(s.plan is not None and s.critic_ok)
        if not plan_ok:
            s.plan, trace.plan = None, None  # no safe plan after the retries
        trace.decision = decide(incident, s.mon, s.dryrun, plan_ok, self.plant.stats)
        self._step(trace, "restraint", t, True, **trace.decision.model_dump())

    def _do_gate(self, s, trace, incident, stall, args):
        t = time.perf_counter()
        trace.autonomy = self.autonomy
        s.gated = True
        reasons = gate_reasons(trace, self.plant, self.autonomy)
        if reasons:
            trace.gate = "advisory" if self.autonomy == "advisory" else "awaiting_approval"
            trace.gate_reasons = reasons
        else:
            trace.gate, trace.gate_reasons = "auto_executed", ["routine ACT: critic and dry-run passed; you can override"]
        self._step(trace, "gate", t, True, gate=trace.gate, reasons=trace.gate_reasons, autonomy=self.autonomy)

    def _do_executor(self, s, trace, incident, stall, args):
        t = time.perf_counter()
        if not (s.critic_ok and s.dryrun and s.dryrun["ok"] and trace.gate == "auto_executed"):
            # unreachable through legal(): kept as a hard stop in case the legality rules ever change
            self._step(trace, "executor", t, False, blocked="plan has not passed critic, dry run and gate")
            s.done = True
            return
        plan = executable_plan(trace)
        tokens = {}
        if self.leases:
            got = self.leases.acquire(incident.id, self.risk, plan_resources(plan))
            tokens = got["tokens"]
            if got["refused"]:  # another incident took them since the critic ran: re-plan without them
                self._step(trace, "arbiter", t, False, refused=got["refused"], note="resources taken by a more urgent incident")
                s.excluded |= set(got["refused"])
                trace.replans += 1
                return self._new_round(s, trace)
            if got["preempted"]:
                self._step(trace, "arbiter", t, True, preempted=got["preempted"],
                           note="higher risk: these resources move to this incident; the others re-plan")
        for aid in list(plan.assignee_ids) + [r.agv_id for r in plan.agv_routes]:
            if aid in self.sim.agents and aid not in trace.dispatch:
                trace.dispatch[aid] = list(self.sim.agents[aid].pos)
        try:
            s.expected = execute(plan, incident, self.plant, self.sim, fleet=self.fleet, tokens=tokens,
                                 key=f"{incident.id}:r{s.round}")
        except PermissionError as err:  # a stale fencing token: the lease was lost in the meantime
            self._step(trace, "arbiter", t, False, refused=[str(err)], note="fencing token refused by the fleet")
            trace.replans += 1
            return self._new_round(s, trace)
        s.executed = True
        if incident.kind == "quality_defect" and getattr(self, "mes", None) is not None:  # contain: hold the units
            self.mes.hold_units(incident.station_id, int((incident.sensors or {}).get("units_affected") or 1),
                                f"{incident.id}: {(incident.sensors or {}).get('defect_code', 'defect')}")
        self._step(trace, "executor", t, all(e["path"] for e in s.expected.values()),
                   eta_s={a: e["eta_s"] for a, e in s.expected.items()})
        if stall and s.round == 1 and plan.agv_routes:
            route = plan.agv_routes[min(stall[0], len(plan.agv_routes) - 1)]
            self.sim.inject_stall(route.agv_id, self.sim.tick + stall[1])
        self.sim.run(VERIFY_TICKS)

    def _do_verifier(self, s, trace, incident, stall, args):
        t = time.perf_counter()
        mismatches = verify(self.sim, s.expected)
        self._step(trace, "verifier", t, not mismatches, mismatches=mismatches)
        s.verified = not mismatches
        if not mismatches:
            return
        trace.replans += 1
        for m in mismatches:
            s.excluded.add(m["agent"])
            if self.sim.agents[m["agent"]].status == "stalled":
                self.sim.stop(m["agent"], status="fault")
        self._new_round(s, trace)

    def _new_round(self, s, trace) -> None:
        if s.round >= MAX_ATTEMPTS:
            trace.decision = RestraintDecision(decision="ESCALATE", score=0.0, reasons=["verifier mismatches persisted"])
            s.done = True
            return
        # a new round: the old plan is void; the candidates without the failed agents can be re-ranked
        s.round += 1
        s.plan = s.plan_source = s.critic_ok = s.dryrun = s.expected = s.verified = None
        s.attempts, s.feedback, s.gated, s.executed = 0, None, False, False
        trace.decision, trace.gate, trace.gate_reasons = None, "none", []
        if s.cands is not None:
            s.cands = {k: [c for c in v if c.id not in s.excluded] for k, v in s.cands.items()}
        s.cands_stale = True

    def _check_preempted(self, s, trace) -> None:
        """A more urgent incident took some of our resources: drop them and plan again."""
        lost = self.leases.take_preempted(trace.incident.id) if self.leases else set()
        if not lost or s.done:
            return
        self._step(trace, "arbiter", time.perf_counter(), False, lost=sorted(lost),
                   note="pre-empted by a higher-risk incident: re-plan without these resources")
        s.excluded |= lost
        trace.replans += 1
        self.leases.release_all(trace.incident.id)
        self._new_round(s, trace)

    def _do_safe_mode(self, s, trace, incident, stall, args):
        t = time.perf_counter()
        trace.safe_mode = True
        plan = fallback_plan(incident, s.cands)
        trace.plan = s.plan = plan
        self._step(trace, "safe_mode", t, False, error=str(args.get("error", "Nightshift unavailable"))[:200])
        trace.decision = RestraintDecision(decision="ESCALATE", score=0.0,
                                           reasons=["Nightshift unavailable: safe mode, rule-based fallback plan"])
        self._step(trace, "restraint", time.perf_counter(), True, **trace.decision.model_dump())
        s.done = True
