"""The standard trace envelope (app/tracefmt.py): every step names its action, summaries, hand-offs and status."""

import json
from pathlib import Path

from app.api import run_request
from app.tracefmt import envelope, fill_targets

ROOT = Path(__file__).resolve().parents[1]
FIELDS = {"trace_id", "agent_name", "action", "input_summary", "output_summary", "from_agent", "target_agent",
          "confidence", "retry_count", "status"}


def test_envelope_statuses_and_summaries():
    e = envelope("r1", "critic", False, {"attempt": 2, "version": 2, "violations": [{"rule_id": "R-CROSSING"}]},
                 subject="part_shortage at F30", prev="planner")
    assert FIELDS <= set(e) and e["status"] == "needs_revision" and e["retry_count"] == 1
    assert "R-CROSSING" in e["output_summary"] and e["from_agent"] == "planner" and e["trace_id"] == "trace-r1"
    assert envelope("r1", "monitor", True, {"p_real": 0.9, "summary": "real"})["confidence"] == 0.9
    assert envelope("r1", "gate", True, {"gate": "awaiting_approval"})["status"] == "awaiting_human"
    ev = envelope("a1", "critic", False, {"kept": [1, 2], "dropped": [3]})
    assert ev["status"] == "partial_success" and "dropped 1" in ev["output_summary"]


def test_fill_targets_links_each_step_to_the_next(tmp_path):
    p = tmp_path / "trace.jsonl"
    rows = [{"incident": "A", "agent_name": a, "target_agent": None} for a in ("reflex", "monitor", "planner")]
    rows.insert(1, {"incident": "B", "agent_name": "reflex", "target_agent": None})
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    fill_targets(p)
    got = [json.loads(line) for line in p.read_text().splitlines()]
    assert [r["target_agent"] for r in got] == ["monitor", "response", "planner", "response"]


def test_a_run_trace_carries_the_envelope_end_to_end():
    (ROOT / "logs" / "trace-t-envelope.jsonl").unlink(missing_ok=True)  # one run per file for this check
    r = run_request({"run_id": "t-envelope", "input": {"kind": "part_shortage", "station": "F30", "seed": 42},
                     "options": {"sample_mode": True}})
    recs = [json.loads(line) for line in open(ROOT / r["log_file"])]
    mine = [x for x in recs if x.get("run_id") == "t-envelope"]
    assert mine and all(FIELDS <= set(x) for x in mine)
    assert mine[0]["from_agent"] is None and mine[-1]["target_agent"] == "response"
    assert all(a["target_agent"] == b["agent_name"] for a, b in zip(mine, mine[1:]))
