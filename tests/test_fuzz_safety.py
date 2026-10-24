"""Property-based fuzzing (seeded, standard library): random plans against the critic and the runtime monitor.

Properties:
  P1 a plan that drives an AGV over a crossing faster than 0.5 m/s is always rejected (R-CROSSING)
  P2 a plan faster than 1.5 m/s is always rejected (R-SAFE-SPEED)
  P3 a robot-cell or repair job given to someone without valid lockout/tagout is always rejected (R-LOTO)
  P4 every plan the critic approves, once executed, causes no runtime violation (AGV in a fence with a person,
     fast crossing, collision) in the next 40 ticks
"""

import random

import pytest

from app.agents.executor import agv_legs, execute
from app.agents.safety_critic import review
from app.contracts import Plan
from app.sim.grid import Grid
from app.sim.invariants import InvariantMonitor
from app.sim.offline import offline_plant
from app.sim.pathing import plan_path
from app.sim.sim import Sim

KINDS = [("part_shortage", "F30"), ("part_shortage", "T20"), ("agv_fault", "F20"), ("spill", "F20"),
         ("overheat", "F10"), ("robot_cell_fault", "B10"), ("torque_fault", "F30"), ("quality_defect", "E10"),
         ("near_miss", "F30")]
PLANT, GRID = offline_plant(), Grid.from_generator()


def random_plan(rng, inc, sim):
    workers = [a for a in sim.agents if a.startswith("worker:")]
    agvs = [a for a, ag in sim.agents.items() if a.startswith("agv:") and ag.status not in ("fault", "held")]
    people = rng.sample(workers, rng.randint(0, 2))
    routes = []
    for a in rng.sample(agvs, rng.randint(0, 1)):
        routes.append({"agv_id": a, "to_station": inc.station_id or "F30", "speed_mps": rng.choice([0.5, 1.0, 1.5, 2.0]),
                       "crossing_speed_mps": rng.choice([None, 0.3, 0.5, 1.0])})
    ids = people + [r["agv_id"] for r in routes]
    return Plan(incident_id=inc.id, assignee_ids=ids or [workers[0]], agv_routes=routes, priority="high", rationale="fuzz")


def crosses_fast(plan, sim):
    for r in plan.agv_routes:
        agv = sim.agents[r.agv_id]
        pickup, target = agv_legs(r, PLANT, sim)
        path = (plan_path(sim.grid, agv.pos, pickup, 0) or []) + (plan_path(sim.grid, pickup, target, 0) or [])
        speed = r.crossing_speed_mps if r.crossing_speed_mps is not None else r.speed_mps
        if any(sim.grid.is_crossing(*c) for c in path) and speed > 0.5:
            return True
    return False


@pytest.mark.parametrize("seed", range(int(__import__("os").environ.get("FUZZ_SEEDS", "6"))))
def test_critic_properties_and_approved_plans_are_safe_at_runtime(seed):
    rng = random.Random(seed)
    approved = 0
    for trial in range(40):
        kind, station = rng.choice(KINDS)
        sim = Sim.from_plant(PLANT, GRID, seed=rng.randint(1, 10_000), shift="B")
        kw = {"agv_id": "agv:plant:AGV-3"} if kind == "agv_fault" else {}
        inc = sim.inject(kind, PLANT, station_id=station, **kw)
        from app.agents.reflex import reflex
        reflex(sim, inc, PLANT)
        plan = random_plan(rng, inc, sim)
        rules = {v.rule_id for v in review(plan, inc, PLANT, sim).violations}
        if crosses_fast(plan, sim):
            assert "R-CROSSING" in rules, (seed, trial, plan)                                   # P1
        if any(r.speed_mps > 1.5 for r in plan.agv_routes):
            assert "R-SAFE-SPEED" in rules                                                     # P2
        if kind in ("overheat", "robot_cell_fault"):
            for wid in plan.assignee_ids:
                w = PLANT.entity(wid)
                if w and w["type"] == "worker" and not PLANT.cert_valid(w, "lockout_tagout"):
                    assert "R-LOTO" in rules                                                   # P3
        if not rules:
            approved += 1
            sim.monitor = InvariantMonitor(PLANT, enforce=False, log_path=None)
            execute(plan, inc, PLANT, sim)
            sim.run(40)
            bad = [v for v in sim.monitor.violations if v["rule"] in ("I1", "I2", "I4")]
            assert not bad, (seed, trial, plan, bad)                                           # P4
    assert approved >= 3  # the property P4 was really exercised


def test_monitor_stops_an_agv_in_a_fence_with_a_person_and_reports_it():
    sim = Sim.from_plant(PLANT, GRID, seed=1, shift="B")
    worker = next(a for a in sim.agents.values() if a.kind == "worker")
    agv = next(a for a in sim.agents.values() if a.kind == "agv")
    x, y = worker.pos
    sim.set_geofence({(x + dx, y + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)}, key="T", kind="spill")
    agv.pos, agv.status = (x + 1, y), "moving"  # a bug elsewhere put a moving AGV next to the person, inside the fence
    sim.monitor = InvariantMonitor(PLANT, log_path=None)
    sim.monitor.check(sim)
    assert sim.monitor.violations[0]["rule"] == "I1" and agv.status != "moving"
