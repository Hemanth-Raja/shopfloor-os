"""Lab in the loop: an applied change is measured on the incidents that follow, against the Lab's prediction."""
from types import SimpleNamespace

import pytest

from app.lab.loop import LabLoop
from app.sim.grid import Grid


def result(policy_type="move_spill_kit", station="F20", significant=True):
    return {"lab_id": "lab-1", "significant": significant, "effect_pct": -8.0, "ci_low_pct": -11.0, "ci_high_pct": -4.0,
            "hypothesis": {"hypothesis": "An extra spill kit at F20.", "policy": {"type": policy_type, "station": station}},
            "predicted_saving": {"per_incident_s": 30.0, "at_station_s": 90.0, "at_station_n": 4}}


def trace(iid, kind, x, y, station, dispatched=True):
    return SimpleNamespace(incident=SimpleNamespace(id=iid, kind=kind, x=x, y=y, station_id=station, zone_id="FINAL"),
                           steps=[SimpleNamespace(step="executor", ok=dispatched)])


@pytest.fixture(scope="module")
def grid():
    return Grid.load(None)


def test_apply_needs_a_significant_result():
    with pytest.raises(ValueError):
        LabLoop().apply(result(significant=False))


def test_incidents_after_apply_are_measured_against_the_prediction(grid):
    from app.data import generate as g
    loop = LabLoop()
    s = loop.apply(result())
    st = {x[0]: x for x in g.STATIONS}["F20"]
    assert s["applied"]["kind"] == "spill" and s["applied"]["cell"] == [st[3], st[4] + 2] and s["n"] == 0
    row = loop.measure(trace("SIM-1", "spill", st[3], st[4] + 1, "F20"), grid)
    assert row["cells_with"] <= row["cells_without"] and row["saving_s"] == 12 * (row["cells_without"] - row["cells_with"])
    assert loop.measure(trace("SIM-2", "overheat", st[3], st[4], "F20"), grid) is None  # another kind
    assert loop.measure(trace("SIM-3", "spill", st[3], st[4], "F20", dispatched=False), grid) is None  # nobody sent
    waiting = trace("SIM-5", "spill", st[3], st[4], "F20", dispatched=False)
    waiting.gate = "approved"  # dispatched later, by your Approve
    assert loop.measure(waiting, grid) is not None
    loop.measure(trace("SIM-1", "spill", st[3], st[4] + 1, "F20"), grid)  # counted once
    s = loop.summary()
    assert s["n"] == 2 and s["at_station_n"] == 2 and s["predicted_saving_s"] == 30.0
    assert loop.undo() == {"applied": None} and loop.measure(trace("SIM-4", "spill", 1, 1, "F20"), grid) is None
