"""Autonomous shift: leases and arbitration, queue order, Sentinel dedupe/correlation/triage, determinism."""

from types import SimpleNamespace

import pytest

from app.autonomy.dispatcher import Dispatcher, incident_risk
from app.autonomy.leases import LeaseBook
from app.autonomy.sentinel import Sentinel, Triage
from app.dynamic import DynamicOrchestrator
from tests.conftest import FakePlanner


# ---------------------------------------------------------------- leases

def test_leases_refuse_equal_risk_preempt_lower_and_expire():
    now = [0.0]
    book = LeaseBook(ttl_s=60, clock=lambda: now[0])
    assert book.acquire("A", 20, ["agv:plant:AGV-5"])["granted"] == ["agv:plant:AGV-5"]
    assert book.acquire("B", 20, ["agv:plant:AGV-5"])["refused"] == ["agv:plant:AGV-5"]  # equal risk: no
    assert book.blocked_for("B", 20) == {"agv:plant:AGV-5"} and book.blocked_for("C", 130) == set()
    got = book.acquire("C", 130, ["agv:plant:AGV-5"])  # people at risk: pre-empts
    assert got["preempted"] == {"agv:plant:AGV-5": "A"} and book.take_preempted("A") == {"agv:plant:AGV-5"}
    assert book.holder("agv:plant:AGV-5").incident == "C"
    now[0] = 61.0  # no deadlocks: a forgotten lease expires
    assert book.holder("agv:plant:AGV-5") is None and book.acquire("B", 1, ["agv:plant:AGV-5"])["granted"]
    assert book.release_all("B") == ["agv:plant:AGV-5"] and book.snapshot() == []


# ---------------------------------------------------------------- queue

def inc(kind, id_, severity="medium"):
    return SimpleNamespace(id=id_, kind=kind, severity=severity, station_id="F30")


class Done:
    def __init__(self, inc, risk):
        self.inc = inc

    def run_steps(self, incident, stall=None):
        yield "reflex"
        return None


def test_queue_runs_people_first_then_cost_and_urgent_jumps_in():
    stats = {"part_shortage": {"avg_cost_usd": 5350}, "quality_defect": {"avg_cost_usd": 420}}
    assert incident_risk(inc("spill", "x"), stats) > incident_risk(inc("part_shortage", "y"), stats) \
        > incident_risk(inc("quality_defect", "z"), stats)
    started = []
    d = Dispatcher(lambda i, r: Done(i, r), stats, workers=1, on_event=lambda k, data: k == "started" and started.append(data["incident"]))
    d.submit(inc("quality_defect", "Q"))
    d.submit(inc("part_shortage", "P"))
    d.step()
    assert started == ["P"]  # most urgent first, one slot
    d.submit(inc("spill", "S"))
    d.step()
    assert "S" in started  # people at risk jump in although the slot is busy
    while not d.idle():
        d.step()
    assert started[-1] == "Q" and {r["incident"] for r in d.done} == {"P", "Q", "S"}


# ---------------------------------------------------------------- Sentinel

def raiser():
    made = []

    def raise_incident(spec):
        made.append(spec)
        return SimpleNamespace(id=f"I{len(made)}", **spec)
    return raise_incident, made


def test_sentinel_dedupes_correlates_and_ignores_normal_readings():
    s, (raise_incident, made) = Sentinel(log_path=None), raiser()
    assert s.process({"type": "line_stock", "station": "F30", "minutes_left": 9}, 1, raise_incident) is None  # normal
    a = s.process({"type": "agv_status", "agv": "AGV-4", "fault_code": "E52", "feeding": "F30", "station": "F30"}, 2,
                  raise_incident)
    assert s.process({"type": "agv_status", "agv": "AGV-4", "fault_code": "E52", "feeding": "F30", "station": "F30"}, 5,
                     raise_incident) is None  # same alarm again
    assert s.process({"type": "line_stock", "station": "F30", "minutes_left": 2}, 8, raise_incident) is None  # its effect
    assert [m["kind"] for m in made] == ["agv_fault"] and a.id == "I1"
    assert s.stats["deduped"] == 1 and s.stats["correlated"] == 1 and s.stats["ignored"] == 1
    s.close("I1")  # handled: the next shortage there is a new incident
    assert s.process({"type": "line_stock", "station": "F30", "minutes_left": 2}, 9, raise_incident) is not None


def test_ambiguous_signals_are_triaged_and_people_risk_is_never_dismissed():
    def triage(lane, messages, schema, **kw):
        assert schema is Triage and kw["role"] == "sentinel"
        return SimpleNamespace(data=Triage(raise_incident=False, likely_false_alarm=True, reason="looks like glare"),
                               model="lightning")
    s, (raise_incident, made) = Sentinel(triage, log_path=None), raiser()
    assert s.process({"type": "torque", "station": "F10", "nok_in_row": 1}, 1, raise_incident) is None  # dismissed
    assert s.process({"type": "camera", "station": "F20", "what": "fluid", "confidence": 0.6}, 2, raise_incident)
    assert [m["kind"] for m in made] == ["spill"] and s.stats["dismissed"] == 1  # the spill is raised anyway


def test_repeat_failures_at_a_station_raise_severity():
    s, (raise_incident, made) = Sentinel(log_path=None), raiser()
    for i, sev in enumerate(("low", "low", "low")):
        s.process({"type": "quality", "station": "E10", "defect": "seal_gap", "units": 1}, 1 + i * 200, raise_incident)
    assert [m["severity"] for m in made] == ["low", "low", "medium"] and "incident 3" in made[2]["note"]


# ---------------------------------------------------------------- arbitration inside the orchestrator

def orch(plant, sim, book, risk, inc_):
    return DynamicOrchestrator(plant, sim, FakePlanner(), trace_path=None, run_sql=lambda sql: [[1, 0, 1.0, 1.0]],
                               leases=book, risk=risk)


def test_a_resource_held_by_a_more_urgent_incident_is_not_offered_or_used(plant, sim):
    book = LeaseBook()
    book.acquire("SPILL-1", 130, ["worker:plant:W-18", "agv:plant:AGV-5"])
    i = sim.inject("part_shortage", plant, station_id="F30")
    trace = orch(plant, sim, book, 32, i).handle(i)
    cands = next(s for s in trace.steps if s.step == "candidates").detail
    assert "worker:plant:W-18" not in cands["workers"] and "agv:plant:AGV-5" not in cands["agvs"]
    assert set(cands["held_by_other_incidents"]) == {"worker:plant:W-18", "agv:plant:AGV-5"}
    assert book.holder("agv:plant:AGV-5").incident == "SPILL-1"  # still theirs


def test_a_preempted_incident_replans_without_the_lost_resource(plant, sim):
    book = LeaseBook()
    i = sim.inject("part_shortage", plant, station_id="F30")
    o = orch(plant, sim, book, 32, i)
    steps = o.run_steps(i)
    for _ in range(4):  # reflex, monitor, candidates, planner
        next(steps)
    taken = o.state.plan.agv_routes[0].agv_id
    book.acquire(i.id, 32, [taken])
    book.acquire("SPILL-1", 130, [taken])  # a more urgent incident takes the AGV
    trace = None
    try:
        while True:
            next(steps)
    except StopIteration as stop:
        trace = stop.value
    assert any(s.step == "arbiter" and taken in s.detail["lost"] for s in trace.steps) and trace.replans >= 1
    assert taken not in [r.agv_id for r in trace.plan.agv_routes]


# ---------------------------------------------------------------- the whole shift, offline

def run_shift(seed):
    from app.autonomy.shift import Shift
    from app.engines.playbooks import PlaybookStore
    from app.sim.grid import Grid
    from app.sim.offline import offline_plant
    from app.sim.sim import Sim
    plant = offline_plant()
    sim = Sim.from_plant(plant, Grid.from_generator(), seed=seed, shift="B")
    memory = PlaybookStore(path=None)

    def make(i, risk, leases, lock):
        return DynamicOrchestrator(plant, sim, FakePlanner(), trace_path=None, memory=memory, leases=leases, risk=risk,
                                   sim_lock=lock, run_sql=lambda sql: [[3, 1, 2.0, 900.0]])
    sh = Shift(plant, sim, make, minutes=4, seed=seed, workers=2, rate_per_min=3, memory=memory)
    sh.sentinel.log_path = None
    return sh.run_headless()


def test_a_shift_is_deterministic_for_a_seed_and_handles_overlapping_incidents():
    a, b = run_shift(5), run_shift(5)
    strip = lambda r: [{k: v for k, v in x.items() if k != "seconds"} for x in r["results"]]  # noqa: E731
    assert strip(a) == strip(b) and a["incidents"] >= 8 and a["max_concurrent"] >= 2
    assert a["auto_resolved"] + len(a["waiting_for_a_person"]) + len(a["escalations"]) >= a["incidents"] - 1
    assert all(r["route"][0] == "reflex" for r in a["results"])
