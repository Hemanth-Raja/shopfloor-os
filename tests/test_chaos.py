"""Chaos scenarios (fake Nightshift, no network): every failure keeps its property and nothing unsafe runs."""

import pytest

from app.lab import chaos


@pytest.mark.parametrize("sc", chaos.SCENARIOS, ids=[s["name"] for s in chaos.SCENARIOS])
def test_scenario_keeps_its_property(sc):
    fake = chaos.FakeNightshift(sc["profile"])
    with chaos.fake_world(fake):
        r = chaos.run_incident(sc.get("kind", "part_shortage"), sc.get("station", "F30"), run_sql=sc.get("run_sql"),
                               **sc.get("inject", {}))
    assert sc["check"](r), r
    assert not r["unsafe_executed"]


def test_many_incidents_on_flaky_models_all_finish_safely():
    out = chaos.overload_shift(minutes=1, p_fail=0.3)
    assert out["incidents"] and not out["unfinished"] and not out["unsafe_executed"]
