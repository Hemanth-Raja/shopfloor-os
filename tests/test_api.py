"""The JSON run contract (app/api.py): sample mode runs real agent logic; errors are structured."""

import json
from pathlib import Path

from app.api import run_request

EXAMPLES = Path(__file__).resolve().parents[1] / "input_examples"


def test_examples_run_in_sample_mode_and_differ():
    results = [run_request(json.loads(p.read_text())) for p in sorted(EXAMPLES.glob("example_*.json"))]
    assert len(results) >= 3 and all(r["status"] == "success" for r in results)
    decisions = [r["output"]["decision"] for r in results]
    assert len(set(decisions)) >= 2  # different inputs, different outcomes
    for r in results:
        assert r["output"]["collisions"] == 0 and r["log_file"].startswith("logs/trace-")
        assert {a["name"] for a in r["agents"]} >= {"reflex", "monitor", "candidates", "planner", "critic", "restraint"}
        assert Path(Path(__file__).resolve().parents[1] / r["log_file"]).exists()


def test_unrecorded_input_still_runs_real_logic():
    r = run_request({"run_id": "t-unrecorded", "input": {"kind": "part_shortage", "station": "T20", "seed": 99},
                     "options": {"sample_mode": True}})
    assert r["status"] == "success" and "rule-based (sample mode)" in r["models"]
    assert r["output"]["plan"]["agv_routes"][0]["to_station"] == "T20"


def test_errors_are_structured_not_crashes():
    bad = run_request({"run_id": "t-bad", "input": {"kind": "meteor"}, "options": {"sample_mode": True}})
    assert bad["status"] == "error" and bad["error"]["type"] == "InvalidInput" and not bad["error"]["recoverable"]
    station = run_request({"run_id": "t-st", "input": {"kind": "spill", "station": "Z99"}, "options": {"sample_mode": True}})
    assert station["status"] == "error" and "Z99" in station["error"]["message"]
