"""Playbook memory: outcomes become lessons that stop the planner repeating a mistake."""

from app.engines.playbooks import PlaybookStore
from app.orchestrator import Orchestrator
from app.sim.sim import Sim

from conftest import FakePlanner


def test_second_incident_uses_the_learned_lesson(plant, grid, tmp_path):
    memory = PlaybookStore(tmp_path / "playbooks.json")
    sim = Sim.from_plant(plant, grid, seed=7, shift="B")
    first = Orchestrator(plant, sim, FakePlanner(), trace_path=None, memory=memory).handle(
        sim.inject("part_shortage", plant, station_id="F30"))
    assert first.critic_rejections == 1
    book = memory.recall("part_shortage", "F30")
    assert [l["rule_id"] for l in book["lessons"]] == ["R-CROSSING"] and book["success_rate"] == 1.0

    sim2 = Sim.from_plant(plant, grid, seed=8, shift="B")
    llm = FakePlanner()
    second = Orchestrator(plant, sim2, llm, trace_path=None, memory=memory).handle(
        sim2.inject("part_shortage", plant, station_id="F30"))
    assert second.critic_rejections == 0 and llm.calls == 1  # the lesson saved a model call
    assert second.steps[2].step == "memory" and second.steps[2].detail["lessons"] == ["R-CROSSING"]


def test_store_persists_and_ledger_is_append_only(plant, grid, tmp_path):
    path = tmp_path / "pb.json"
    memory = PlaybookStore(path)
    sim = Sim.from_plant(plant, grid, seed=7, shift="B")
    Orchestrator(plant, sim, FakePlanner(), trace_path=None, memory=memory).handle(
        sim.inject("overheat", plant, station_id="F10", false_alarm=True))
    before = [dict(e) for e in memory.ledger]
    reloaded = PlaybookStore(path)
    assert reloaded.playbooks == memory.playbooks
    reloaded.recall("overheat", "F10")
    assert reloaded.ledger[: len(before)] == before and len(reloaded.ledger) == len(before) + 1
    assert [e["seq"] for e in reloaded.ledger] == list(range(1, len(reloaded.ledger) + 1))


def test_recall_falls_back_to_same_kind_elsewhere(plant, grid, tmp_path):
    memory = PlaybookStore(tmp_path / "pb.json")
    sim = Sim.from_plant(plant, grid, seed=7, shift="B")
    Orchestrator(plant, sim, FakePlanner(), trace_path=None, memory=memory).handle(
        sim.inject("part_shortage", plant, station_id="F30"))
    assert memory.recall("part_shortage", "T20")["key"] == "part_shortage@F30"
    assert memory.recall("spill", "F20") is None


def test_lessons_live_in_hmem_and_get_outcome_feedback(plant, grid, tmp_path):
    memory = PlaybookStore(tmp_path / "pb.json")
    for seed in (7, 8):
        sim = Sim.from_plant(plant, grid, seed=seed, shift="B")
        Orchestrator(plant, sim, FakePlanner(), trace_path=None, memory=memory).handle(
            sim.inject("part_shortage", plant, station_id="F30"))
    lesson = memory._lesson("part_shortage", "F30", "R-CROSSING")
    assert lesson["source"].startswith("SIM-7-") and lesson["rule_id"] == "R-CROSSING"
    assert lesson["confidence"] == 0.75  # 0.7 + helpful: the recalled lesson held on the second incident
    actions = [e["action"] for e in memory.hmem.ledger.history(lesson["id"])]
    assert actions[0] == "create" and "read" in actions and actions[-1] == "feedback"
