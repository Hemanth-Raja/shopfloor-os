"""The four extra incident kinds: robot-cell fault, nutrunner torque fault, pedestrian near miss, quality defect."""

import pytest

from app.agents.candidates import find_candidates
from app.agents.safety_critic import review
from app.api import run_request
from app.contracts import Plan
from app.engines.citations import default_index
from app.sim.grid import Grid
from app.sim.offline import offline_plant
from app.sim.sim import Sim


@pytest.fixture()
def world():
    plant = offline_plant()
    return plant, Sim.from_plant(plant, Grid.from_generator(), seed=42, shift="B")


def run(**inp):
    out = run_request({"input": inp, "options": {"sample_mode": True}})
    assert out["status"] == "success", out
    return out["output"]


def test_robot_cell_fault_needs_lockout_and_a_cell_card(world):
    plant, sim = world
    inc = sim.inject("robot_cell_fault", plant, station_id="B10")
    assert inc.machine_id == "machine:plant:M-01" and inc.zone_id == "BODY"
    plan = Plan(incident_id=inc.id, assignee_ids=["worker:plant:W-12"], priority="high", rationale="nearest")
    v = review(plan, inc, plant, sim, default_index())
    assert [x.rule_id for x in v.violations] == ["R-LOTO"]
    assert v.violations[0].citations[0].ref.startswith("29 CFR 1910.147")
    out = run(kind="robot_cell_fault", station="B10")
    assert out["decision"] == "ACT" and out["plan"]["assignee_ids"] == ["worker:plant:W-19"]


def test_torque_fault_needs_an_audit_and_a_single_nok_is_verified(world):
    plant, sim = world
    inc = sim.inject("torque_fault", plant, station_id="F10")
    cands = find_candidates(plant, sim, inc)
    assert any("torque_audit" in c.skills for c in cands["workers"])  # always offered
    plan = Plan(incident_id=inc.id, assignee_ids=["worker:plant:W-16"], priority="high", rationale="tool swap")
    v = review(plan, inc, plant, sim, default_index())
    assert v.violations[0].rule_id == "R-TORQUE-QA" and v.violations[0].citations[0].ref == "SOP-TORQUE-01 §2"
    assert run(kind="torque_fault", station="F10")["decision"] == "ACT"
    assert run(kind="torque_fault", station="F10", false_alarm=True)["decision"] == "VERIFY"


def test_near_miss_holds_the_agv_and_needs_a_supervisor(world):
    plant, sim = world
    inc = sim.inject("near_miss", plant, station_id="F30")
    held = sim.agents[inc.agv_id]
    assert held.status == "held" and (inc.x, inc.y) == held.pos
    plan = Plan(incident_id=inc.id, assignee_ids=["worker:plant:W-13"], priority="high", rationale="first aid",
                agv_routes=[{"agv_id": inc.agv_id, "to_station": "F30", "speed_mps": 0.5, "crossing_speed_mps": 0.5}])
    rules = {x.rule_id for x in review(plan, inc, plant, sim).violations}
    assert {"R-NEAR-MISS"} <= rules
    out = run(kind="near_miss", station="F30")
    assert out["decision"] == "ACT" and out["plan"]["assignee_ids"] == ["worker:plant:W-18"]


def test_quality_defect_is_contained_by_an_inspector():
    out = run(kind="quality_defect", station="E10", severity="low", note="seal gap on two cars")
    assert out["decision"] == "ACT" and out["plan"]["assignee_ids"] == ["worker:plant:W-20"]


def test_new_kinds_wait_for_a_person_when_someone_enters_a_hazard_or_signs_off():
    for kind, station, text in (("robot_cell_fault", "B10", "robot cell fault hazard"), ("near_miss", "F30", "signs off")):
        out = run(kind=kind, station=station)
        assert out["gate"] == "awaiting_approval" and any(text in r for r in out["gate_reasons"])
    assert run(kind="quality_defect", station="E10")["gate"] == "auto_executed"
