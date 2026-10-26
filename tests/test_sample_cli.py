"""run.py ask, lab and handover in SAMPLE_MODE=true: offline, no Zetaris or Nightshift, exit cleanly."""

from __future__ import annotations

import json

import pytest

import run as cli
from app.handover import handover, rule_based_chat, rule_based_note


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("sample mode must not call Zetaris or Nightshift")
    from app import runtime
    monkeypatch.setenv("SAMPLE_MODE", "true")
    monkeypatch.setattr(runtime, "run_sql", refuse)
    monkeypatch.setattr(runtime, "chat", refuse)


def test_ask_prints_floor_evidence_without_crashing(no_network, capsys):
    cli.ask("Why does F10 keep stopping?")
    out = capsys.readouterr().out
    assert "Recommendation:" in out and " floor: " in out and "models " in out


def test_lab_runs_offline_and_writes_nothing(no_network, capsys):
    cli.lab(write=True)
    out = capsys.readouterr().out
    assert "Hypothesis (rule-based)" in out and "95% CI" in out
    row = json.loads(out.split("Proposal:", 1)[1])
    assert "SAMPLE_MODE: nothing written" in out and row["status"] in ("proposed", "not_significant")


def test_rule_based_handover_reads_every_slice_without_a_model(tmp_path):
    events = [{"seq": 0, "kind": "shift_started", "data": {}}]
    events += [{"seq": i, "kind": "finished", "data": {"incident": f"SIM-1-{i:03d}", "kind": "spill",
                                                       "gate": "awaiting_approval" if i == 3 else "auto_executed"}}
               for i in range(1, 90)]
    events += [{"seq": 100, "kind": "lease", "data": {"resource": "agv:plant:AGV-2"}},
               {"seq": 101, "kind": "shift_finished", "data": {}}]
    out = handover(rule_based_chat(events), events=events, log_path=tmp_path / "m.jsonl", shift_log_dir=tmp_path)
    assert out["completed"] and out["chunks"] == 3 and out["note"] == rule_based_note(events)
    assert "89 incidents finished (spill 89); 88 ran on their own" in out["note"]
    assert "Waiting for a person: SIM-1-003." in out["note"] and "AGV-2 1" in out["note"]
    assert out["tokens"]["markovian_prompt_tokens"] < out["tokens"]["standard_prompt_tokens"]
