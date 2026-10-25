from datetime import datetime, timezone

from app.askhistory import AskHistory, norm


def answer(ask_id, summary, claims, rows):
    return {"ask_id": ask_id, "question": "Why does F10 keep stopping?", "summary": summary,
            "findings": [{"claim": c, "evidence": ["e1"]} for c in claims],
            "evidence": [{"id": "e1", "type": "sql", "task": "stops at F10", "rows": rows}]}


def test_same_question_is_compared_with_its_last_answer(tmp_path):
    h = AskHistory(tmp_path / "asks.jsonl")
    first = h.add(answer("ask-1", "Overheat alarms.", ["12 overheats"], 12), datetime(2026, 10, 25, 9, tzinfo=timezone.utc))
    assert "previous" not in first and first["asked_at"].startswith("2026-10-25T09")
    again = h.add({**answer("ask-2", "Overheat alarms, mostly false.", ["14 overheats"], 14),
                   "question": "why does f10 keep stopping"})
    assert again["previous"]["ask_id"] == "ask-1"
    c = again["changes"]
    assert c["summary_changed"] and c["new_findings"] == ["14 overheats"] and c["gone_findings"] == ["12 overheats"]
    assert (c["rows_then"], c["rows_now"], c["same_evidence"]) == (12, 14, False)
    assert [r["ask_id"] for r in h.listing()] == ["ask-2", "ask-1"] and h.listing()[0]["changed"]
    reloaded = AskHistory(tmp_path / "asks.jsonl")  # survives a restart
    assert reloaded.get("ask-1")["summary"] == "Overheat alarms." and reloaded.last()["ask_id"] == "ask-2"


def test_norm_ignores_case_and_punctuation():
    assert norm("Are overheat  alarms REAL?") == norm("are overheat alarms real")
