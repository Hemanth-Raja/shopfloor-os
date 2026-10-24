"""Dynamic orchestrator: guardrails in code, and different incidents taking different routes."""

from types import SimpleNamespace

import pytest

from app import dynamic
from app.dynamic import DynamicOrchestrator, RouteChoice
from app.engines.playbooks import PlaybookStore
from app.llm import LLMUnavailable
from app.orchestrator import Orchestrator
from app.sim.sim import Sim
from tests.conftest import FakePlanner

HISTORY = lambda sql: [[12, 9, 4.2, 900.0]]  # noqa: E731 - fake Zetaris: 12 past incidents, 9 false alarms


class Router:
    """A scripted model router: returns the scripted choices in order, then the first legal agent."""

    def __init__(self, script=(), unavailable=False):
        self.script, self.unavailable, self.seen = list(script), unavailable, []

    def __call__(self, lane, messages, schema, **kw):
        assert schema is RouteChoice and lane == "fast"
        if self.unavailable:
            raise LLMUnavailable("breaker open")
        import json
        legal = [a["name"] for a in json.loads(messages[1]["content"])["choose_next_agent_from"]]
        self.seen.append(legal)
        pick = self.script.pop(0) if self.script else legal[0]
        return SimpleNamespace(data=RouteChoice(next_agent=pick, reason="scripted"), model="fake-router")


def orch(plant, sim, router=None, **kw):
    return DynamicOrchestrator(plant, sim, kw.pop("llm", FakePlanner()), trace_path=None, router_llm=router,
                               run_sql=kw.pop("run_sql", HISTORY), **kw)


def steps(trace):
    return [s.step for s in trace.steps]


def test_reflex_always_runs_first(plant, sim):
    trace = orch(plant, sim, Router(["planner"])).handle(sim.inject("part_shortage", plant, station_id="F30"))
    assert steps(trace)[0] == "reflex" and trace.path[0] == {"hop": 0, "agent": "reflex", "by": "code",
                                                              "reason": "the reflex always runs first"}


def test_illegal_and_unknown_choices_are_rejected_and_logged(plant, sim):
    router = Router(["executor", "launch_missiles"])  # executor before any plan; an agent that does not exist
    trace = orch(plant, sim, router).handle(sim.inject("part_shortage", plant, station_id="F30"))
    rejected = [s.detail["rejected"] for s in trace.steps if s.step == "router"]
    assert rejected == ["executor", "launch_missiles"]
    assert all("executor" not in legal for legal in router.seen[:2])


def test_executor_only_after_critic_dryrun_and_gate(plant, sim):
    trace = orch(plant, sim, Router()).handle(sim.inject("part_shortage", plant, station_id="F30"))
    order = steps(trace)
    ex = order.index("executor")
    last_plan = max(i for i, s in enumerate(order[:ex]) if s in ("planner", "playbook", "verify_plan"))
    assert order[last_plan:ex] == [order[last_plan], "critic", "dryrun", "restraint", "gate"]
    assert trace.steps[order.index("critic", last_plan)].ok and trace.gate == "auto_executed"


def test_human_gate_holds_everything_in_supervised_mode(plant, sim):
    trace = orch(plant, sim, Router(), autonomy="supervised").handle(sim.inject("part_shortage", plant, station_id="F30"))
    assert trace.gate == "awaiting_approval" and "executor" not in steps(trace)


def test_nightshift_down_forces_safe_mode(plant, sim):
    trace = orch(plant, sim, Router(unavailable=True), llm=FakePlanner()).handle(
        sim.inject("part_shortage", plant, station_id="F30"))
    assert trace.safe_mode and trace.decision.decision == "ESCALATE" and "planner" not in steps(trace)
    assert any(s.step == "router" and "unavailable" in s.detail["note"] for s in trace.steps)


def test_hop_budget_ends_in_escalate(plant, sim, monkeypatch):
    monkeypatch.setattr(dynamic, "HOP_BUDGET", 3)
    trace = orch(plant, sim).handle(sim.inject("part_shortage", plant, station_id="F30"))
    assert trace.decision.decision == "ESCALATE" and "hop budget" in trace.decision.reasons[0]
    assert "executor" not in steps(trace)


def test_false_alarm_route_skips_the_planner(plant, sim):
    llm = FakePlanner()
    trace = orch(plant, sim, llm=llm).handle(sim.inject("overheat", plant, station_id="F10", false_alarm=True))
    assert [p["agent"] for p in trace.path][:5] == ["reflex", "monitor", "investigator", "candidates", "verify_plan"]
    assert trace.decision.decision == "VERIFY" and llm.calls == 0
    assert trace.steps[2].detail["false_alarm_share"] == 0.75


def test_known_case_uses_the_playbook_not_the_planner(plant, grid, tmp_path):
    memory = PlaybookStore(path=tmp_path / "pb.json")
    first = orch(plant, Sim.from_plant(plant, grid, seed=42, shift="B"), memory=memory)
    t1 = first.handle(first.sim.inject("part_shortage", plant, station_id="F30"))
    assert "planner" in steps(t1) and t1.decision.decision == "ACT"
    llm = FakePlanner()
    second = orch(plant, Sim.from_plant(plant, grid, seed=42, shift="B"), memory=memory, llm=llm)
    t2 = second.handle(second.sim.inject("part_shortage", plant, station_id="F30"))
    assert "playbook" in steps(t2) and "planner" not in steps(t2) and llm.calls == 0
    assert t2.critic_rejections == 0 and t2.decision.decision == "ACT"  # the learned crossing speed is in the pattern


def test_verifier_mismatch_goes_back_to_candidates(plant, sim):
    trace = orch(plant, sim).handle(sim.inject("agv_fault", plant, station_id="F30", agv_id="agv:plant:AGV-3"),
                                    stall=(0, 2))
    agents = [p["agent"] for p in trace.path]
    i = agents.index("verifier")
    assert agents[i + 1] == "candidates" and trace.replans == 1 and trace.decision.decision == "ACT"


def test_linear_mode_is_unchanged(plant, sim):
    trace = Orchestrator(plant, sim, FakePlanner(), trace_path=None).handle(sim.inject("part_shortage", plant, station_id="F30"))
    assert trace.mode == "linear" and trace.path == [] and "investigator" not in steps(trace)
