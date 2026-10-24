"""Anytime planning: optimal assignment (v1) at once, the model's plan (v2) only if it is safe and better."""

import itertools
import random
import time
from types import SimpleNamespace

from app.agents.assign import assign_plan, hungarian
from app.agents.candidates import find_candidates
from app.contracts import Plan
from app.dynamic import DynamicOrchestrator


def test_hungarian_is_optimal_against_brute_force():
    rng = random.Random(3)
    for _ in range(200):
        n = rng.randint(1, 3)
        m = rng.randint(n, 6)
        c = [[rng.choice([rng.randint(0, 40), 1e6]) for _ in range(m)] for _ in range(n)]
        p = hungarian(c)
        best = min(sum(c[i][q[i]] for i in range(n)) for q in itertools.permutations(range(m), n))
        assert len(set(p)) == n and abs(sum(c[i][p[i]] for i in range(n)) - best) < 1e-6


def test_rules_plan_fills_every_role_and_respects_certificates(plant, sim):
    for kind, station, kw in (("torque_fault", "F10", {}), ("robot_cell_fault", "B10", {}),
                              ("part_shortage", "F30", {}), ("near_miss", "F30", {})):
        inc = sim.inject(kind, plant, station_id=station, **kw)
        t = time.perf_counter()
        plan, info = assign_plan(inc, find_candidates(plant, sim, inc, k=6), plant, sim)
        assert plan is not None and time.perf_counter() - t < 1.0, (kind, info)
        from app.agents.safety_critic import review
        assert review(plan, inc, plant, sim).ok, (kind, review(plan, inc, plant, sim).violations)


class V2:
    """A fake model: returns `plan_fn(payload)` after `delay` seconds."""

    def __init__(self, plan_fn, delay=0.0):
        self.plan_fn, self.delay, self.calls = plan_fn, delay, 0

    def __call__(self, lane, messages, schema, **kw):
        import json
        self.calls += 1
        time.sleep(self.delay)
        payload = json.loads(messages[1]["content"])
        return SimpleNamespace(data=self.plan_fn(payload), model="fake-nemotron")


def run(plant, sim, llm, kind="part_shortage", station="F30"):
    inc = sim.inject(kind, plant, station_id=station)
    return DynamicOrchestrator(plant, sim, llm, trace_path=None).handle(inc)


def refine(trace):
    return next(s for s in trace.steps if s.step == "refine").detail


def test_unsafe_model_plan_is_rejected_and_the_rules_plan_kept(plant, sim):
    def fast_at_crossing(p):
        agv = p["agv_candidates"][0]["id"]
        return Plan(incident_id="x", assignee_ids=[p["worker_candidates"][0]["id"], agv], priority="high", rationale="go",
                    agv_routes=[{"agv_id": agv, "to_station": "F30", "speed_mps": 1.0, "crossing_speed_mps": 1.0}])
    trace = run(plant, sim, V2(fast_at_crossing))
    d = refine(trace)
    assert d["chosen"] == "v1" and "R-CROSSING" in d["v2"]["violations"] and trace.critic_rejections == 1
    assert trace.decision.decision == "ACT" and trace.plan.agv_routes[0].crossing_speed_mps == 0.5


def test_a_safe_and_faster_model_plan_is_swapped_in(plant, sim):
    def lean(p):  # one AGV only: fewer committed resources -> better score when safe
        agv = p["agv_candidates"][0]["id"]
        return Plan(incident_id="x", assignee_ids=[agv], priority="high", rationale="AGV alone",
                    agv_routes=[{"agv_id": agv, "to_station": "F30", "speed_mps": 1.0, "crossing_speed_mps": 0.5}])
    trace = run(plant, sim, V2(lean))
    d = refine(trace)
    assert d["chosen"] == "v2" and d["v2"]["score"] < d["v1"]["score"] and trace.plan.assignee_ids == d["v2"]["assignees"]


def test_a_model_that_is_too_slow_never_blocks_the_rules_plan(plant, sim, monkeypatch):
    from app import models
    monkeypatch.setitem(models.CFG["defaults"], "anytime_wait_s", 0.2)
    t = time.perf_counter()
    trace = run(plant, sim, V2(lambda p: None, delay=2.0))
    assert time.perf_counter() - t < 1.5 and refine(trace)["chosen"] == "v1" and trace.decision.decision == "ACT"
