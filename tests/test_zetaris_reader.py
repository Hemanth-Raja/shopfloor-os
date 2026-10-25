"""Agent reads use the read-only Zetaris login; writes and the operator CLI use the operator login."""

from types import SimpleNamespace

from app import zetaris


def fake_post(seen):
    def post(url, headers, json, timeout):
        seen.append(headers["Authorization"])
        return SimpleNamespace(ok=True, json=lambda: {"data": [["1"]]})
    return post


def test_reads_use_the_agent_key_and_writes_the_operator_key(monkeypatch):
    seen = []
    monkeypatch.setattr(zetaris.requests, "post", fake_post(seen))
    monkeypatch.setenv("ZETARIS_REST_URL", "http://z")
    monkeypatch.setenv("ZETARIS_ORG_ID", "1")
    monkeypatch.setenv("ZETARIS_API_KEY", "op-key")
    monkeypatch.setenv("ZETARIS_AGENT_API_KEY", "ro-key")
    zetaris.run_sql("SELECT 1")
    zetaris.run_sql("INSERT INTO sf_ops.experiments VALUES (1)", write=True)
    assert seen == ["Bearer ro-key", "Bearer op-key"] and zetaris.reader() == "read-only role"
    monkeypatch.delenv("ZETARIS_AGENT_API_KEY")
    zetaris.run_sql("SELECT 1")  # no agent login yet: falls back, and health says so
    assert seen[-1] == "Bearer op-key" and zetaris.reader().startswith("operator")


def test_errors_never_carry_a_key(monkeypatch):
    monkeypatch.setenv("ZETARIS_AGENT_API_KEY", "ro-secret")
    assert "ro-secret" not in zetaris._scrub("denied for ro-secret")


def test_reader_role_grants_select_on_every_sf_datasource(monkeypatch):
    monkeypatch.setattr(zetaris, "run_sql", lambda sql, **k: [["ADMIN"]])
    sql = zetaris.reader_role_sql("agent@shopfloor.local")
    assert sql[0].startswith("CREATE ROLE 'sf_agent_reader'")
    assert sum("GRANT SELECT ON DATASOURCE NAMESPACE sf_" in s for s in sql) == 4
    assert not any("INSERT" in s for s in sql) and sql[-1] == "ASSIGN USER 'agent@shopfloor.local' TO ROLE 'sf_agent_reader'"
    monkeypatch.setattr(zetaris, "run_sql", lambda sql, **k: [["SF_AGENT_READER"]])
    assert not zetaris.reader_role_sql()[0].startswith("CREATE")  # re-running is safe


def test_live_the_agent_login_cannot_write():
    """Integration: runs only once ZETARIS_AGENT_API_KEY is in .env and Zetaris is up."""
    import os
    import pytest
    from dotenv import load_dotenv
    load_dotenv()
    if not os.environ.get("ZETARIS_AGENT_API_KEY") or os.environ.get("SF_INTEGRATION") != "1":
        pytest.skip("integration: set SF_INTEGRATION=1 with an agent login in .env")
    assert zetaris.run_sql("SELECT COUNT(*) FROM sf_ops.incidents")
    with pytest.raises(RuntimeError):
        zetaris.run_sql("INSERT INTO sf_ops.experiments (experiment_id) VALUES ('READONLY-PROBE')")
