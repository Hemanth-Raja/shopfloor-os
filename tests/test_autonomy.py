"""Human-in-the-loop gate: Auto / Supervised / Advisory, approve dispatches, reject rolls back."""

from app.contracts import Plan, RestraintDecision, Trace
from app.orchestrator import Orchestrator, gate_reasons

from conftest import FakePlanner


def run(plant, sim, kind, station, autonomy="auto", **kw):
    inc = sim.inject(kind, plant, station_id=station, **kw)
    return Orchestrator(plant, sim, FakePlanner(), trace_path=None, autonomy=autonomy).handle(inc)


def steps(trace):
    return [s.step for s in trace.steps]


def test_auto_executes_a_routine_act(sim, plant):
    t = run(plant, sim, "part_shortage", "F30")
    assert t.decision.decision == "ACT" and t.gate == "auto_executed" and "executor" in steps(t)
    assert t.dispatch and all(isinstance(v, list) for v in t.dispatch.values())


def test_person_entering_a_hazard_waits_even_in_auto(sim, plant):
    t = run(plant, sim, "spill", "F20")
    assert t.gate == "awaiting_approval" and "executor" not in steps(t)
    assert any("hazard" in r for r in t.gate_reasons)
    assert not any(a.status == "moving" for a in sim.agents.values())  # nothing moved


def test_supervised_and_advisory_never_move_on_their_own(sim, plant):
    sup = run(plant, sim, "part_shortage", "F30", autonomy="supervised")
    assert sup.gate == "awaiting_approval" and "executor" not in steps(sup)
    adv = run(plant, sim, "part_shortage", "F30", autonomy="advisory")
    assert adv.gate == "advisory" and "executor" not in steps(adv)


def test_lapsed_certificate_needs_a_human(sim, plant):
    inc = sim.inject("part_shortage", plant, station_id="F30")
    trace = Trace(incident=inc, plan=Plan(incident_id=inc.id, assignee_ids=["worker:plant:W-21"], priority="high",
                                          rationale="r"),
                  decision=RestraintDecision(decision="ACT", score=0.9, reasons=[]))
    assert any("W-21 has a lapsed certificate (forklift)" in r for r in gate_reasons(trace, plant, "auto"))


def test_server_approve_dispatches_and_reject_rolls_back(monkeypatch, plant):
    monkeypatch.setenv("SAMPLE_MODE", "true")
    from app import server
    app = server.App(offline=True, replay=True)
    app.bus.publish = lambda *a: None
    waiting = run(app.plant, app.sim, "spill", "F20")
    app.traces[waiting.incident.id] = waiting
    rec = app.approve({"incident_id": waiting.incident.id, "approve": True})
    assert rec["gate"] == "approved" and any(app.sim.agents[a].status == "moving" for a in waiting.dispatch)

    auto = run(app.plant, app.sim, "part_shortage", "F30")
    app.traces[auto.incident.id] = auto
    app.sim.run(3)
    moved = {a: tuple(start) for a, start in auto.dispatch.items() if tuple(start) != app.sim.agents[a].pos}
    rec = app.approve({"incident_id": auto.incident.id, "approve": False})
    assert rec["gate"] == "rolled_back"
    app.sim.run(60)
    assert moved and all(app.sim.agents[a].pos == start for a, start in moved.items())  # back where they started
