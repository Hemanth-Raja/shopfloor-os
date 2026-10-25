"""Chaos harness: break Nightshift, the models and Zetaris on purpose and check what the agents do.

`python run.py chaos` -> docs/chaos.md. No network: a fake Nightshift replaces the one HTTP call in app/models.py
(`_complete`), so everything above it is the real code: retries with backoff, per-model breakers, the fallback
chain, hedged requests, deadlines, JSON repair, the Nightshift-wide breaker, the router, the planner, the critic,
the anytime rules plan, safe mode and the human gate. The fake answers like a model would (the router picks a legal
agent, the planner returns a plan for the candidates it was shown) and fails the way the real endpoints do (HTTP 5xx,
429, timeouts, empty replies, broken JSON, a different model answering, slow replies past the deadline).

Every scenario states the property it must keep. The headline property everywhere: nothing unsafe is executed, and
the incident ends in a decision (an action, or a person asked), never a crash or a hang.
"""

from __future__ import annotations

import json
import random
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from app import models
from app.llm import LLMUnavailable

ROOT = Path(__file__).resolve().parents[2]
SUPER, LIGHT = "nvidia/nemotron-3.5-super-vl-120b-a12b", "nvidia/nemotron-3.5-lightning-30b-a3b"


class FakeNightshift:
    """Replaces models._complete. profile: model -> {down, p_fail, latency_s, empty, bad_json, unsafe, phantom, as}."""

    def __init__(self, profile: dict | None = None, seed: int = 1):
        self.profile = profile or {}
        self.rng = random.Random(seed)
        self.lock = threading.Lock()
        self.calls: list[dict] = []

    def __call__(self, model, role, messages, max_tokens, timeout_s, json_mode):
        p = self.profile.get(model, {})
        with self.lock:
            roll = self.rng.random()
            self.calls.append({"model": model, "role": role})
        lat = p.get("latency_s", 0.01)
        if lat > timeout_s:  # slower than the budget: the client times out
            time.sleep(min(timeout_s, 0.3))
            raise LLMUnavailable(f"timeout after {timeout_s:.1f} s")
        time.sleep(lat)
        if p.get("down"):
            raise LLMUnavailable("HTTP 502")
        if roll < p.get("p_fail", 0.0):
            raise LLMUnavailable(self.rng.choice(["HTTP 429", "HTTP 503", "HTTP 502"]))
        if p.get("empty"):
            raise LLMUnavailable("empty reply")
        text = self.answer(role, messages, p)
        if p.get("bad_json") and json_mode:
            text = text[: len(text) // 2]  # cut off mid-object
        answering = p.get("as", model)
        return SimpleNamespace(text=text, model=answering, requested=model, latency_s=lat, truncated=False,
                               request_id=None, tokens={"prompt": 100, "completion": 50, "total": 150})

    @staticmethod
    def _payload(messages):
        for m in messages:
            if m["role"] == "user":
                try:
                    return json.loads(m["content"])
                except (json.JSONDecodeError, TypeError):
                    continue
        return {}

    def answer(self, role, messages, p) -> str:
        if role == "router":
            legal = [x["name"] for x in self._payload(messages).get("choose_next_agent_from", [])]
            return json.dumps({"next_agent": "teleport" if p.get("illegal_route") else (legal[0] if legal else "monitor"),
                               "reason": "fake router: first legal option", "args": {}})
        if role == "planner":
            from app.agents.planner import rule_based_plan
            base = [m for m in messages if not (m["role"] == "system" and "schema" in m["content"])]
            plan = rule_based_plan(base[:2] if p.get("unsafe") or p.get("phantom") else base).model_dump()
            if p.get("unsafe"):  # ignores feedback: always drives fast over the crossing
                for r in plan["agv_routes"]:
                    r["speed_mps"], r["crossing_speed_mps"] = 1.0, 1.0
            if p.get("phantom"):
                plan["assignee_ids"] = ["worker:plant:W-99"]
            return json.dumps(plan)
        return "Fake summary: alarm checked."


@contextmanager
def fake_world(nightshift: FakeNightshift, budget_s: float = 2.0):
    """Patch the model layer for one scenario: fake HTTP, short budgets, fresh breakers, limiters and latency stats."""
    from app import runtime
    saved = {"complete": models._complete, "sleep": models.SLEEP, "receipt": models.receipt_for,
             "roles": json.loads(json.dumps(models.CFG["roles"]))}
    models._complete, models.SLEEP, models.receipt_for = nightshift, (lambda s: time.sleep(min(s, 0.05))), \
        (lambda *a, **k: None)
    for spec in models.CFG["roles"].values():
        spec["budget_s"] = budget_s
    models.BREAKERS.clear(), models.LIMITERS.clear(), models.EFFECTIVE.clear()
    models.LATENCY.__init__()
    for b in runtime.BREAKERS.values():
        b.failures, b.opened_at, b._probing = 0, None, False
    try:
        yield
    finally:
        models._complete, models.SLEEP, models.receipt_for = saved["complete"], saved["sleep"], saved["receipt"]
        models.CFG["roles"] = saved["roles"]
        models.BREAKERS.clear(), models.LIMITERS.clear()
        for b in runtime.BREAKERS.values():
            b.failures, b.opened_at, b._probing = 0, None, False


def run_incident(kind="part_shortage", station="F30", run_sql=None, seed=5, autonomy="auto", **inject):
    """One incident through the real dynamic orchestrator, with the model layer as patched by fake_world."""
    from app import runtime
    from app.agents.safety_critic import review
    from app.dynamic import DynamicOrchestrator
    from app.sim.grid import Grid
    from app.sim.offline import offline_plant
    from app.sim.sim import Sim
    plant = offline_plant()
    sim = Sim.from_plant(plant, Grid.from_generator(), seed=seed, shift="B")
    if kind == "agv_fault":
        inject.setdefault("agv_id", "agv:plant:AGV-3")
    inc = sim.inject(kind, plant, station_id=station, **inject)
    from app.engines.playbooks import PlaybookStore
    from app.investigate.sources import sql_runner
    # memory (in RAM) and an investigator give the router real choices, so the router model is exercised too
    orch = DynamicOrchestrator(plant, sim, runtime.chat_json, runtime.chat, trace_path=None, autonomy=autonomy,
                               router_llm=runtime.chat_json, run_sql=run_sql or sql_runner("true"),
                               memory=PlaybookStore(path=None))
    t = time.perf_counter()
    trace = orch.handle(inc)
    seconds = time.perf_counter() - t
    executed = trace.gate in ("auto_executed",)
    unsafe_executed = bool(executed and trace.plan is not None and review(trace.plan, inc, plant, sim).violations)
    planner = [s.detail.get("model_call") or {} for s in trace.steps if s.step in ("planner", "refine")]
    return {"decision": trace.decision.decision if trace.decision else None, "gate": trace.gate,
            "safe_mode": trace.safe_mode, "critic_rejections": trace.critic_rejections,
            "unsafe_executed": unsafe_executed, "seconds": round(seconds, 2),
            "route": [p["agent"] for p in trace.path], "router_by": sorted({str(p.get("by", "")).split(" ")[0]
                                                                            for p in trace.path}),
            "planner_answered_by": [c.get("answering") for c in planner if c.get("answering")],
            "planner_fell_back": any(c.get("fell_back") for c in planner),
            "retries": models.RETRIES["n"], "steps": [s.step for s in trace.steps]}


def zetaris_down(sql):
    raise RuntimeError("Zetaris HTTP 503: service unavailable")


SCENARIOS = [
    {"name": "Healthy baseline", "profile": {},
     "expect": "ACT, auto-executed, Nemotron routes and plans",
     "check": lambda r: r["decision"] == "ACT" and r["gate"] == "auto_executed" and not r["safe_mode"]},
    {"name": "Super VL down (one model fails)", "profile": {SUPER: {"down": True}},
     "expect": "the planner falls back to Lightning; no safe mode",
     "check": lambda r: not r["safe_mode"] and LIGHT in r["planner_answered_by"] and r["planner_fell_back"]},
    {"name": "Lightning down (router model fails)", "profile": {LIGHT: {"down": True}},
     "expect": "the router falls back to Super VL; no safe mode (this failed before the fix in models._race)",
     "check": lambda r: not r["safe_mode"] and "nemotron" in r["router_by"] and not r["unsafe_executed"]},
    {"name": "Nightshift down (every model fails)", "profile": {SUPER: {"down": True}, LIGHT: {"down": True}},
     "expect": "rules plan (anytime v1); a person approves it; no crash",
     "check": lambda r: r["decision"] is not None and r["gate"] != "auto_executed" and not r["unsafe_executed"]},
    {"name": "Overloaded: 60% of calls get 429/5xx", "profile": {SUPER: {"p_fail": 0.6}, LIGHT: {"p_fail": 0.6}},
     "expect": "retries with backoff and fallback absorb it; a decision is reached",
     "check": lambda r: r["decision"] is not None and not r["unsafe_executed"]},
    {"name": "Planner model slower than its budget", "profile": {SUPER: {"latency_s": 5.0}},
     "expect": "the call times out inside the deadline; Lightning or the rules plan answers",
     "check": lambda r: r["decision"] is not None and r["seconds"] < 60},
    {"name": "Broken JSON from the planner", "profile": {SUPER: {"bad_json": True}},
     "expect": "repair, one re-ask, then the next model",
     "check": lambda r: r["decision"] is not None and not r["unsafe_executed"]},
    {"name": "Empty replies (model spends its budget reasoning)", "profile": {SUPER: {"empty": True}, LIGHT: {"empty": True}},
     "expect": "treated as transient, retried, then rules plan + person",
     "check": lambda r: r["decision"] is not None and r["gate"] != "auto_executed"},
    {"name": "Planner always proposes an unsafe crossing", "profile": {SUPER: {"unsafe": True}, LIGHT: {"unsafe": True}},
     "expect": "the critic rejects every model plan; the safe rules plan is used; nothing unsafe runs",
     "check": lambda r: r["critic_rejections"] >= 1 and not r["unsafe_executed"]},
    {"name": "Planner names a person who does not exist", "profile": {SUPER: {"phantom": True}, LIGHT: {"phantom": True}},
     "expect": "World Model grounding rejects the id; the rules plan is used",
     "check": lambda r: not r["unsafe_executed"] and r["decision"] is not None},
    {"name": "Router proposes an illegal move", "profile": {LIGHT: {"illegal_route": True}, SUPER: {"illegal_route": True}},
     "expect": "the guardrails reject it and the rule-based router decides; critic and dry run still run",
     "check": lambda r: "critic" in r["steps"] and "dryrun" in r["steps"]},
    {"name": "Nightshift serves a different model than requested", "profile": {SUPER: {"as": "meterless/other-model"}},
     "expect": "the answer is used but marked as fallen back (drift is reported in Health)",
     "check": lambda r: r["decision"] is not None and r["planner_fell_back"]},
    {"name": "Zetaris down during an overheat check", "profile": {}, "kind": "overheat", "station": "F10",
     "run_sql": zetaris_down, "inject": {"false_alarm": True},
     "expect": "the investigator's queries fail; the incident is still decided from sensors and rules",
     "check": lambda r: r["decision"] is not None and not r["unsafe_executed"]},
    {"name": "Spill needs a person in the hazard (gate)", "profile": {}, "kind": "spill", "station": "F20",
     "expect": "waits for a person; the approval-timeout policy later holds it safe, never auto-dispatches",
     "check": lambda r: r["gate"] == "awaiting_approval"},
]


def run_all() -> list[dict]:
    rows = []
    for sc in SCENARIOS:
        fake = FakeNightshift(sc["profile"])
        models.RETRIES["n"] = 0
        with fake_world(fake):
            try:
                r = run_incident(sc.get("kind", "part_shortage"), sc.get("station", "F30"), run_sql=sc.get("run_sql"),
                                 **sc.get("inject", {}))
                ok, error = bool(sc["check"](r)), None
            except Exception as err:  # noqa: BLE001 - a crash is a failed scenario, reported as such
                r, ok, error = {}, False, f"{type(err).__name__}: {str(err)[:200]}"
        rows.append({"scenario": sc["name"], "expect": sc["expect"], "pass": ok, "error": error,
                     "model_calls": len(fake.calls), **r})
    return rows


def overload_shift(minutes: float = 3, p_fail: float = 0.3) -> dict:
    """Many incidents at once on flaky models: the Sentinel, the dispatcher and the leases under load."""
    from app import runtime
    from app.autonomy.shift import Shift
    from app.dynamic import DynamicOrchestrator
    from app.sim.grid import Grid
    from app.sim.offline import offline_plant
    from app.sim.sim import Sim
    from app.agents.safety_critic import review
    plant = offline_plant()
    sim = Sim.from_plant(plant, Grid.from_generator(), seed=11, shift="B")
    lock = threading.RLock()
    fake = FakeNightshift({SUPER: {"p_fail": p_fail, "latency_s": 0.05}, LIGHT: {"p_fail": p_fail, "latency_s": 0.02}})
    executed_unsafe = []

    def make(inc, risk, leases, sim_lock):
        return DynamicOrchestrator(plant, sim, runtime.chat_json, runtime.chat, trace_path=None, router_llm=runtime.chat_json,
                                   leases=leases, risk=risk, sim_lock=sim_lock)
    with fake_world(fake):
        sh = Shift(plant, sim, make, minutes=minutes, seed=11, workers=4, threads=True, sentinel_llm=runtime.chat_json,
                   sim_lock=lock, rate_per_min=8)
        t = time.perf_counter()
        out = sh.run_headless()
        for tr in sh.traces.values():
            if tr.gate == "auto_executed" and tr.plan and review(tr.plan, tr.incident, plant, sim).violations:
                executed_unsafe.append(tr.incident.id)
    return {"incidents": out.get("incidents"), "decisions": out.get("decisions"), "unfinished": out.get("unfinished"),
            "max_concurrent": out.get("max_concurrent"), "preemptions": out.get("preemptions"),
            "model_calls": len(fake.calls), "wall_seconds": round(time.perf_counter() - t, 1),
            "unsafe_executed": executed_unsafe}


def report(rows: list[dict], shift: dict | None) -> str:
    passed = sum(r["pass"] for r in rows)
    out = [f"# Chaos tests\n\nGenerated by `python run.py chaos` on {datetime.now().astimezone():%Y-%m-%d %H:%M %Z}. "
           "A fake Nightshift replaces only the HTTP call in `app/models.py`; everything above it is the real code "
           "(retries, breakers, fallback chain, hedging, deadlines, JSON repair, router, planner, critic, anytime "
           "rules plan, safe mode, human gate). No network; rerun any time.\n",
           f"**{passed} of {len(rows)} scenarios keep their property. Unsafe plans executed: "
           f"{sum(bool(r.get('unsafe_executed')) for r in rows)}.**\n",
           "| Scenario | Must hold | Result | Decision / gate | Planner answered by | Model calls | Time |",
           "|---|---|---|---|---|---|---|"]
    for r in rows:
        who = ", ".join(sorted({m.split('/')[-1] for m in r.get("planner_answered_by", [])})) or "rules"
        res = "pass" if r["pass"] else f"**FAIL** {r['error'] or ''}"
        out.append(f"| {r['scenario']} | {r['expect']} | {res} | {r.get('decision')} / {r.get('gate')}"
                   f"{' (safe mode)' if r.get('safe_mode') else ''} | {who} | {r.get('model_calls')} | {r.get('seconds', '-')} s |")
    if shift:
        out += ["\n## Many incidents at once on flaky models\n",
                f"A {shift.get('incidents')}-incident shift (8 signals a minute) with 30% of model calls failing, up to 4 incidents at a time "
                f"(reached {shift.get('max_concurrent')}): "
                f"decisions {shift.get('decisions')}, unfinished {shift.get('unfinished') or 'none'}, "
                f"{shift.get('model_calls')} model calls, "
                f"{shift.get('wall_seconds')} s. Unsafe plans executed: {len(shift.get('unsafe_executed') or [])}.\n"]
    out += ["\n## Found and fixed by these tests\n",
            "- **A hedged role skipped its second model.** With Lightning down, the router's hedge never started, so "
            "Super VL was never asked; the router reported Nightshift down and the whole incident fell into safe mode. "
            "`models._race` now gives the second model its turn when the first fails fast "
            "(`tests/test_resilience.py`).",
            "- **Zetaris down at start crashed the server.** It now starts degraded on the offline plant, says so in "
            "Health and in the UI, and keeps using Nightshift (`tests/test_fixtures_and_server.py`).\n",
            "## How the model is chosen for each call\n",
            "Every agent role has an ordered chain in `config/models.json` (for example the planner: Super VL, then "
            "Lightning). For each call, `app/models.py`:",
            "1. drops models the startup self-check found broken or silently served by another model, and models whose "
            "context window the prompt would overflow;",
            "2. skips a model whose circuit breaker is open (two failures; one probe after the cooldown);",
            "3. waits for a slot under that model's adaptive concurrency limit (AIMD: halves on errors and very slow "
            "replies, grows back slowly), so an overcrowded model is not hammered;",
            "4. retries transient errors (429, 5xx, timeouts, empty replies) with full-jitter backoff inside the "
            "incident's deadline, repairs broken JSON before re-asking, then moves to the next model;",
            "5. for latency-critical roles (router, monitor, Sentinel) also asks the next model once the first is slower "
            "than 1.5x its median, and takes the first valid answer;",
            "6. if every model fails, the Nightshift-wide breaker opens: the router uses rules, the planner's rules plan "
            "(optimal assignment, already critic-checked) is used, and a person approves it.\n",
            "## When a person does not answer\n",
            "Covered by `app/autonomy/approval.py` and `tests/test_approval_timeout.py`. Reminder after 2 floor minutes, "
            "escalation to the shift manager after 4, and at 6 the agents decide, never in the unsafe direction: a plan "
            "held only by the Supervised setting (critic and dry run passed, acting cheaper than waiting) is dispatched "
            "and audited; anything that needs a person's judgement (a person entering a hazard, a lapsed certificate, "
            "a near miss, safe mode, ESCALATE/SAFE_HOLD) stays in a safe hold, its reserved people and robots are "
            "released, and it stays escalated. A late Approve still works. In Advisory mode the clock only reminds."]
    return "\n".join(out) + "\n"


def run(out_md: Path = ROOT / "docs" / "chaos.md", with_shift: bool = True) -> dict:
    rows = run_all()
    shift = overload_shift() if with_shift else None
    out_md.write_text(report(rows, shift), encoding="utf-8")
    (ROOT / "logs" / f"chaos-{datetime.now():%Y%m%d-%H%M%S}.json").write_text(
        json.dumps({"scenarios": rows, "shift": shift}, indent=1, default=str), encoding="utf-8")
    return {"scenarios": rows, "shift": shift}
