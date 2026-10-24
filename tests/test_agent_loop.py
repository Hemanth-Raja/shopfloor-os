"""Simulator, agents and orchestrator, all offline (fake LLM, fixture plant)."""

from app.agents.monitor import assess
from app.agents.reflex import reflex
from app.agents.restraint import decide
from app.agents.safety_critic import review
from app.contracts import AgvRoute, Plan
from app.orchestrator import Orchestrator
from app.sim.pathing import Reservations, path_cells_ok, plan_path

from conftest import FakePlanner


def test_astar_paths_are_valid_and_shortest(grid):
    path = plan_path(grid, (12, 13), (30, 13), 0)
    assert path[0] == (12, 13) and path[-1] == (30, 13)
    assert path_cells_ok(grid, path)
    assert len(path) - 1 == 18  # straight along the south lane
    assert plan_path(grid, (12, 13), (12, 10), 0) is None  # conveyor cell: not walkable


def test_reservation_table_keeps_agents_apart(grid):
    res = Reservations()
    jobs = [("a", (10, 13), (30, 13)), ("b", (30, 13), (10, 13)), ("c", (20, 14), (20, 7)), ("d", (15, 7), (25, 14))]
    paths = {}
    for agent, start, goal in jobs:
        paths[agent] = plan_path(grid, start, goal, 0, res, agent)
        assert paths[agent] is not None and path_cells_ok(grid, paths[agent])
        res.reserve_path(agent, paths[agent], 0)
    horizon = max(len(p) for p in paths.values())
    at = lambda p, t: p[min(t, len(p) - 1)]  # noqa: E731
    for t in range(horizon):
        cells = [at(p, t) for p in paths.values()]
        assert len(cells) == len(set(cells)), f"vertex conflict at tick {t}"
        for a in paths:
            for b in paths:
                if a < b and t and at(paths[a], t) == at(paths[b], t - 1) and at(paths[b], t) == at(paths[a], t - 1):
                    raise AssertionError(f"swap conflict between {a} and {b} at tick {t}")


def test_sim_runs_many_assignments_without_collisions(sim, plant):
    for i, agv in enumerate(a for a in sim.agents.values() if a.kind == "agv"):
        sim.assign(agv.id, plant.station_xy(["T10", "T20", "M10", "F10", "F20", "F30", "E10", "B10"][i]))
    sim.run(80)
    assert sim.collisions == []


def test_reflex_geofences_and_is_fast(sim, plant):
    inc = sim.inject("spill", plant, station_id="F20")
    agv = next(a for a in sim.agents.values() if a.kind == "agv")
    sim.agents[agv.id].pos = (inc.x, inc.y + 1)  # an AGV standing in the spill area
    out = reflex(sim, inc)
    assert out["ms"] < 100
    assert out["fence_cells"] > 0 and agv.id in out["stopped"]


def test_critic_catches_expired_loto(sim, plant):
    inc = sim.inject("overheat", plant, station_id="F10", false_alarm=False)
    plan = Plan(incident_id=inc.id, assignee_ids=["worker:plant:W-22"], priority="high", rationale="night tech")
    verdict = review(plan, inc, plant, sim)
    assert not verdict.ok
    assert [v.rule_id for v in verdict.violations] == ["R-LOTO"]
    assert verdict.violations[0].cfr_ref == "29 CFR 1910.147"


def test_critic_catches_crossing_speed_and_accepts_the_fix(sim, plant):
    inc = sim.inject("part_shortage", plant, station_id="F30")
    fast = Plan(incident_id=inc.id, assignee_ids=["agv:plant:AGV-1"], priority="high", rationale="r",
                agv_routes=[AgvRoute(agv_id="agv:plant:AGV-1", to_station="F30", speed_mps=1.0)])
    verdict = review(fast, inc, plant, sim)
    assert [v.rule_id for v in verdict.violations] == ["R-CROSSING"]
    slow = fast.model_copy(update={"agv_routes": [AgvRoute(agv_id="agv:plant:AGV-1", to_station="F30", speed_mps=1.0,
                                                           crossing_speed_mps=0.5)]})
    assert review(slow, inc, plant, sim).ok


def test_restraint_verifies_a_false_alarm_overheat(sim, plant):
    inc = sim.inject("overheat", plant, station_id="F10", false_alarm=True)
    mon = assess(inc)
    assert mon["verdict"] == "likely_noise" and not mon["sensor_agreement"]
    assert decide(inc, mon, {"ok": True, "reasons": []}, True, plant.stats).decision == "VERIFY"
    real = sim.inject("overheat", plant, station_id="F10", false_alarm=False)
    assert decide(real, assess(real), {"ok": True, "reasons": []}, True, plant.stats).decision == "ACT"


def test_orchestrator_end_to_end_with_critic_retry(sim, plant, tmp_path):
    llm = FakePlanner()
    inc = sim.inject("part_shortage", plant, station_id="F30")
    trace = Orchestrator(plant, sim, llm, trace_path=tmp_path / "trace.jsonl").handle(inc)
    assert trace.critic_rejections == 1 and llm.calls == 2
    assert trace.decision.decision == "ACT" and trace.replans == 0
    steps = [s.step for s in trace.steps]
    assert steps[:3] == ["reflex", "monitor", "candidates"] and steps[-1] == "verifier"
    assert len((tmp_path / "trace.jsonl").read_text().splitlines()) == len(trace.steps)
    assert sim.collisions == []


def test_verifier_triggers_replan_on_agv_stall(sim, plant, tmp_path):
    inc = sim.inject("agv_fault", plant, station_id="F30", agv_id="agv:plant:AGV-3")
    trace = Orchestrator(plant, sim, FakePlanner(), trace_path=tmp_path / "t.jsonl").handle(inc, stall=(0, 2))
    verifier = [s for s in trace.steps if s.step == "verifier"]
    assert trace.replans == 1 and not verifier[0].ok and verifier[-1].ok
    first, second = [s.detail["agv_routes"][0]["agv_id"] for s in trace.steps if s.step == "planner"][::2][:2]
    assert first != second  # the stalled AGV is excluded from the re-plan


def test_orchestrator_safe_mode_when_nightshift_unavailable(sim, plant, tmp_path):
    inc = sim.inject("spill", plant, station_id="F20")
    trace = Orchestrator(plant, sim, FakePlanner(unavailable=True), trace_path=tmp_path / "t.jsonl").handle(inc)
    assert trace.safe_mode and trace.decision.decision == "ESCALATE" and trace.plan is not None
