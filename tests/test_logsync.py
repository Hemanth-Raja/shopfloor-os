"""Agent logs into the data layer: SQL is built from new log lines only, idempotently (no database needed)."""

import json

from app import logsync


def test_statements_cover_steps_and_calls_and_resume_from_offsets(tmp_path):
    step = {"ts": "2026-10-24T10:00:00+00:00", "run_id": "r1", "incident": "SIM-1", "agent": "critic", "ok": False,
            "ms": 1.2, "detail": {"attempt": 1, "violations": [{"rule_id": "R-CROSSING", "detail": "it's fast"}]}}
    (tmp_path / "trace-r1.jsonl").write_text(json.dumps(step) + "\n" + '{"half": ')  # last line still being written
    (tmp_path / "llm.jsonl").write_text(json.dumps({"ts": "2026-10-24T10:00:01+00:00", "lane": "planner",
                                                    "requested_model": "a", "answering_model": "b", "latency_s": 2.5,
                                                    "tokens": {"prompt": 10, "completion": 5}}) + "\n")
    sql, offsets, counts = logsync.statements(tmp_path)
    assert counts == {"agent_steps": 1, "llm_calls": 1}
    text = "\n".join(sql)
    assert "'R-CROSSING'" in text and "it''s fast" in text and "ON CONFLICT (id) DO NOTHING" in text
    again, _, counts2 = logsync.statements(tmp_path, offsets)
    assert counts2 == {"agent_steps": 0, "llm_calls": 0} and len(again) == 1  # only the CREATE TABLE statements


def test_agent_tables_are_shown_only_for_questions_about_the_agents():
    from app.investigate.investigator import catalog_text
    from app.investigate.sqlguard import check
    assert "sf_ops.agent_steps" in catalog_text("Which rule does the critic reject most?")
    assert "sf_ops.agent_steps" not in catalog_text("Why does F10 keep stopping?")
    check("SELECT rules, count(*) FROM sf_ops.agent_steps WHERE agent = 'critic' GROUP BY rules")
