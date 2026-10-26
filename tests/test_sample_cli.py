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
    assert "Hypothesis (nemotron)" in out and "95% CI" in out  # the recorded Lab reply replays
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


def test_lab_on_a_fresh_clone_leaves_the_grid_cache_alone(no_network, monkeypatch, tmp_path, capsys):
    from app.sim import grid as grid_mod
    monkeypatch.setattr(grid_mod, "CACHE", tmp_path / "floor_cells.json")  # a fresh clone: no cached grid
    cli.lab(write=False)
    assert "95% CI" in capsys.readouterr().out and not grid_mod.CACHE.exists()
    assert grid_mod.Grid.load().cols == grid_mod.Grid.from_generator().cols  # the server's grid still loads


def test_a_bad_grid_cache_is_dropped_and_rebuilt(monkeypatch, tmp_path):
    from app.sim import grid as grid_mod
    monkeypatch.setattr(grid_mod, "CACHE", tmp_path / "floor_cells.json")
    grid_mod.CACHE.write_text(json.dumps([["INC-00001", "fluid_spill", "coolant"]]))  # what the old lab bug wrote
    assert grid_mod.Grid.load().cols == grid_mod.Grid.from_generator().cols and not grid_mod.CACHE.exists()
    grid_mod.CACHE.write_text("not json")
    good = [[x, y, "aisle"] for y in range(2) for x in range(3)]
    assert grid_mod.Grid.load(lambda sql: good).cols == 3 and json.loads(grid_mod.CACHE.read_text()) == good
    grid_mod.CACHE.unlink()
    try:
        grid_mod.Grid.load(lambda sql: [["INC-1", "spill", "x"]])
    except ValueError:
        pass
    assert not grid_mod.CACHE.exists()  # rows that do not make a grid are never cached
