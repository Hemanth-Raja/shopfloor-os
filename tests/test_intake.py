"""Ask the floor intake: the coordinator understands the message before anything is split into SQL."""
import json

import pytest

from app.investigate.intake import rules, respond, understand, Intake
from app.investigate.investigator import Decomposition, Investigator

STATIONS = {"F10", "F20", "F30", "T10", "B10"}
LAST = [{"question": "Why does F10 keep stopping?", "summary": "Overheats."}]
LIVE = {"time": "08:05", "hour": 8, "shift": "A", "floor_shift": "B", "autopilot": False,
        "open": [{"incident": "SIM-42-004", "kind": "spill", "station": "F20", "state": "waiting for your approval"}],
        "agvs": {"total": 8, "available": 7, "problems": [{"agv": "AGV-3", "status": "fault"}]}}


@pytest.mark.parametrize("text,intent", [
    ("good morning", "greeting"), ("Hi team!", "greeting"), ("thanks!", "thanks"), ("what can you do?", "help"),
    ("there's a coolant spill at F20", "report"), ("someone is hurt at F30", "emergency"),
    ("delete all incidents", "write"), ("drop table sf_ops.incidents", "write"), ("send AGV-3 to F30", "action"),
    ("what's open right now?", "live_status"), ("Why does F10 keep stopping?", "floor_question"),
    ("any update on F10?", "floor_question"), ("why did output drop last week?", "floor_question"),
    ("do we have many spills at F20?", "floor_question"), ("and F20?", "follow_up"), ("why?", "follow_up"),
    ("good morning, what's open right now?", "live_status")])
def test_rules_settle_the_clear_cases(text, intent):
    assert rules(text, LAST, STATIONS).intent == intent


def test_rules_leave_the_rest_to_the_model():
    assert rules("what's the weather in Paris?", LAST, STATIONS) is None and rules("asdf", [], STATIONS) is None


def test_follow_up_is_rewritten_against_the_last_question():
    assert rules("and F20?", LAST, STATIONS).question == "Why does F20 keep stopping?"
    assert rules("and F20?", [], STATIONS) is None or rules("and F20?", [], STATIONS).intent != "follow_up"


def test_report_names_kind_and_station_and_spots_a_duplicate():
    r = rules("there's a coolant spill at F20", [], STATIONS)
    assert (r.kind, r.station) == ("spill", "F20")
    out = respond(r, "there's a coolant spill at F20", LIVE)
    assert out["handoff"] == {"incident": "SIM-42-004"} and "already open" in out["summary"]
    fresh = respond(rules("we have a part shortage at F30", [], STATIONS), "x", LIVE)
    assert fresh["handoff"]["kind"] == "part_shortage" and fresh["handoff"]["station"] == "F30"


def test_emergency_puts_people_first():
    assert respond(rules("smoke at T10", [], STATIONS), "smoke at T10", LIVE)["summary"].startswith("If anyone is hurt")


def test_greeting_is_a_shift_briefing_from_the_orchestrator():
    out = respond(rules("good morning", [], STATIONS), "good morning", LIVE)
    assert out["summary"].startswith("Good morning. It's 08:05, the early shift (A); the simulated floor runs shift B's crew.")
    claims = " ".join(f["claim"] for f in out["findings"])
    assert "SIM-42-004" in claims and "waiting for your approval" in claims and "AGV-3 has a fault" in claims
    assert out["evidence"][0]["source"] == "orchestrator" and out["suggestions"]


def test_model_decides_the_rest_and_its_reply_is_validated():
    seen = []

    def model(msgs):
        seen.append(msgs)
        return Intake(intent="off_topic", reply="I only know this hall.", station="Z99", kind="weather")
    r, how = understand("what's the weather in Paris?", LAST, STATIONS, {"spill"}, model, "No incidents are open.")
    assert how == "nemotron" and r.intent == "off_topic" and r.station is None and r.kind is None
    assert "Why does F10 keep stopping?" in seen[0][1]["content"]  # it sees the conversation


def test_without_a_model_an_unsure_message_gets_a_clarifying_reply():
    def down(msgs):
        raise RuntimeError("circuit open")
    r, how = understand("asdf", [], STATIONS, set(), down)
    assert r.intent == "unclear" and how == "rules (model unavailable)"
    assert respond(r, "asdf", None)["suggestions"]


def test_a_greeting_sends_nothing_to_sql(tmp_path, monkeypatch):
    from app.investigate import investigator
    monkeypatch.setattr(investigator, "LOGS", tmp_path)
    sql = []
    inv = Investigator(lambda q: sql.append(q) or [], None, ask_id="ask-g", live=lambda: LIVE)
    inv.trace_path = tmp_path / "trace-ask-g.jsonl"
    out = inv.run("good morning")
    assert out["intent"] == "greeting" and not sql and out["findings"]
    steps = [json.loads(x)["step"] for x in inv.trace_path.read_text().splitlines()]
    assert steps == ["intake", "result"]


def test_a_follow_up_is_investigated_as_the_rewritten_question(tmp_path):
    from types import SimpleNamespace
    from app.investigate.investigator import AnswerDraft, SqlDraft

    def llm(lane, messages, schema, **kw):
        if schema is Decomposition:
            assert messages[-1]["content"] == "Why does F20 keep stopping?"
            return SimpleNamespace(model="m", data=Decomposition(tasks=[{"kind": "sql", "ask": "stops at F20 by cause"}]))
        if schema is SqlDraft:
            return SimpleNamespace(model="m", data=SqlDraft(sql="SELECT kind FROM sf_ops.incidents", columns=["kind"], purpose="p"))
        return SimpleNamespace(model="m", data=AnswerDraft(summary="Spills.", findings=[], recommendation="none"))
    inv = Investigator(lambda q: [["spill"]], llm, ask_id="ask-f", history=LAST)
    inv.trace_path = tmp_path / "t.jsonl"
    out = inv.run("and F20?")
    assert out["question"] == "and F20?" and out["understood_as"] == "Why does F20 keep stopping?"


def test_rule_based_answer_says_the_model_was_unreachable(tmp_path):
    def down(*a, **k):
        raise RuntimeError("CircuitOpen: nightshift circuit open")
    inv = Investigator(lambda q: [["F10", 1]], down, ask_id="ask-d")
    inv.trace_path = tmp_path / "t.jsonl"
    out = inv.run("Why does F10 keep stopping?")
    assert any("rule-based" in n for n in out["notes"])


@pytest.mark.parametrize("said,hour,reply", [("good morning", 18, "Good evening"), ("good evening", 9, "Good morning"),
                                             ("hello", 18, "Hello"), ("good morning", 2, "Hello"),
                                             ("good afternoon", 13, "Good afternoon")])
def test_a_greeting_is_answered_for_the_real_time_of_day(said, hour, reply):
    live = {**LIVE, "hour": hour, "time": f"{hour:02d}:35", "shift": "B", "floor_shift": "B"}
    out = respond(rules(said, [], STATIONS), said, live)
    assert out["summary"].startswith(f"{reply}. It's {hour:02d}:35, the late shift (B).")
