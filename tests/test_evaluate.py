"""Eval harness: offline parts are deterministic and the live part works with a stub planner (no network)."""

from app.contracts import Plan
from app.lab import evaluate as ev


def test_planted_plans_are_all_caught_and_approved_plans_are_safe_at_runtime():
    p = ev.planted(seeds=range(1))
    assert p["unsafe"] >= 20 and p["recall"] == 1.0 and p["false_rejects"] == 0
    assert p["approved_with_runtime_event"] == 0
    assert {"overspeed", "fast_crossing", "lapsed_loto", "phantom_station"} <= set(p["by_mutation"])


def test_monitor_beats_the_threshold_alarm_and_is_reproducible():
    a, b = ev.calibration(n=80), ev.calibration(n=80)
    assert a == b and a["brier_monitor"] < a["brier_threshold_alarm"]


def test_live_baseline_with_a_stub_planner_counts_unsafe_first_plans(monkeypatch):
    monkeypatch.setattr(ev, "SCENARIOS", [("part_shortage", "F30")])

    def stub(lane, messages, schema, **kw):
        import json
        payload = json.loads(messages[1]["content"])
        agv = payload["agv_candidates"][0]["id"]
        fast = len(messages) == 2  # first attempt drives too fast; after critic feedback it slows down
        return Plan(incident_id="x", assignee_ids=[payload["worker_candidates"][0]["id"], agv], priority="high",
                    agv_routes=[{"agv_id": agv, "to_station": "F30", "speed_mps": 2.0 if fast else 1.0,
                                 "crossing_speed_mps": 0.5}], rationale="stub")

    lv = ev.live(stub, repeats=2, workers=1)
    assert lv["answered"] == 2 and lv["baseline_unsafe_or_ungrounded"] == 2
    assert lv["fixed_by_critic_feedback"] == 2 and lv["full_loop_unsafe_executed"] == 0
    assert lv["stability"][0]["identical"]
