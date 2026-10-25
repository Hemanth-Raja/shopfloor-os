"""Offline decision check: a saved decision is re-derived with code only, and a tampered one is caught."""
from app.lab import chaos
from app.verify import verify


def _trace(kind="part_shortage", station="F30"):
    from app import runtime
    from app.dynamic import DynamicOrchestrator
    from app.engines.playbooks import PlaybookStore
    from app.investigate.sources import sql_runner
    from app.sim.grid import Grid
    from app.sim.offline import offline_plant
    from app.sim.sim import Sim
    plant = offline_plant()
    sim = Sim.from_plant(plant, Grid.from_generator(), seed=5, shift="B")
    with chaos.fake_world(chaos.FakeNightshift({})):
        inc = sim.inject(kind, plant, station_id=station)
        trace = DynamicOrchestrator(plant, sim, runtime.chat_json, runtime.chat, trace_path=None,
                                    router_llm=runtime.chat_json, run_sql=sql_runner("true"),
                                    memory=PlaybookStore(path=None)).handle(inc)
    return trace, plant, sim


def test_a_real_decision_is_confirmed_without_a_model():
    trace, plant, sim = _trace()
    out = verify(trace, plant, sim, journal=False)
    assert out["verdict"] == "CONFIRMED" and out["model_calls"] == 0, out
    assert {c["check"] for c in out["checks"]} >= {"plan re-checked by the safety critic", "the human gate was respected"}


def test_a_tampered_plan_or_gate_is_named():
    trace, plant, sim = _trace()
    assert trace.plan and trace.plan.agv_routes  # a part shortage sends an AGV
    fast = trace.plan.model_copy(update={"agv_routes": [r.model_copy(update={"speed_mps": 3.0, "crossing_speed_mps": 3.0})
                                                        for r in trace.plan.agv_routes]})
    out = verify(trace.model_copy(update={"plan": fast}), plant, sim, journal=False)
    bad = [c for c in out["checks"] if c["ok"] is False]
    assert out["verdict"] == "DIFFERS" and bad[0]["check"] == "plan re-checked by the safety critic", out
    spill, plant2, sim2 = _trace("spill", "F20")
    assert spill.decision.decision in ("ACT", "VERIFY") and spill.gate != "auto_executed"
    forged = spill.model_copy(update={"gate": "auto_executed"})  # a person had to approve: it may not run alone
    out = verify(forged, plant2, sim2, journal=False)
    assert any(c["check"] == "the human gate was respected" and c["ok"] is False for c in out["checks"]), out
