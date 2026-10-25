"""Ask the floor: SQL guard, offline SQL source, investigator team with a fake model, evidence critic, custom incidents."""

import json
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from app.investigate import investigator as inv_mod
from app.investigate.investigator import AnswerDraft, Decomposition, Finding, Investigator, SqlDraft
from app.investigate.sources import offline_run_sql
from app.investigate.sqlguard import MAX_ROWS, GuardError, check


# ---------------------------------------------------------------- SQL guard

@pytest.mark.parametrize("sql", [
    "DELETE FROM sf_ops.incidents",
    "DROP TABLE sf_ops.incidents",
    "SELECT * FROM sf_ops.incidents; DROP TABLE sf_ops.incidents",
    "INSERT INTO sf_ops.experiments VALUES (1)",
    "SELECT * FROM sf_ops.secrets",
    "SELECT * FROM incidents",
    "SELECT 1",
    "",
    "WITH x AS (SELECT * FROM sf_ops.incidents) UPDATE x SET kind = 'a'",
    "SELECT * FROM sf_ops.incidents /* ; */ ; SET spark.x = 1",
    "REFRESH DATASOURCE sf_ops",
    "SHOW TABLES IN sf_ops",
])
def test_guard_refuses_anything_but_one_whitelisted_select(sql):
    with pytest.raises(GuardError):
        check(sql)


def test_guard_allows_selects_and_caps_the_limit():
    assert check("SELECT kind FROM sf_ops.incidents").endswith(f"LIMIT {MAX_ROWS}")
    assert check("select kind from sf_ops.incidents limit 5000").endswith(f"LIMIT {MAX_ROWS}")
    assert check("SELECT kind FROM sf_ops.incidents LIMIT 7;").endswith("LIMIT 7")
    sql = ("WITH f AS (SELECT agv_id, count(*) AS n FROM sf_ops.incidents WHERE kind = 'agv_fault; drop' GROUP BY agv_id) "
           "SELECT f.agv_id, f.n, a.battery_health_pct FROM f JOIN sf_fleet.agvs a ON a.agv_id = f.agv_id")
    assert "sf_fleet.agvs" in check(sql)  # CTE names and keywords inside strings are fine


# ---------------------------------------------------------------- offline source (SAMPLE_MODE without a fixture)

def test_offline_sql_answers_the_demo_questions():
    rows = offline_run_sql("SELECT station_id, count(*) FROM sf_ops.incidents WHERE station_id = 'F10' GROUP BY station_id")
    assert rows[0][0] == "F10" and rows[0][1] > 100
    hours = offline_run_sql("SELECT hour(ts) AS h, count(*) FROM sf_ops.incidents GROUP BY 1 ORDER BY 2 DESC LIMIT 1")
    assert 0 <= hours[0][0] <= 23


# ---------------------------------------------------------------- investigator with a fake model

def fake_llm(replies):
    calls = []

    def llm(lane, messages, schema, **kw):
        calls.append(schema.__name__)
        data = replies[schema.__name__].pop(0) if isinstance(replies[schema.__name__], list) else replies[schema.__name__]
        if isinstance(data, Exception):
            raise data
        return SimpleNamespace(data=data, model="nvidia/nemotron-test")
    llm.calls = calls
    return llm


@pytest.fixture(autouse=True)
def tmp_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(inv_mod, "LOGS", tmp_path)
    (tmp_path / "trace-SIM-42-001.jsonl").write_text(json.dumps(
        {"step": "restraint", "ok": True, "incident": "SIM-42-001", "detail": {"decision": "ACT", "station": "F10"}}) + "\n")
    return tmp_path


def test_sql_agent_retries_once_with_the_error_and_the_critic_checks_numbers(tmp_logs):
    llm = fake_llm({
        "Decomposition": Decomposition(tasks=[{"kind": "sql", "ask": "downtime by station"}]),
        "SqlDraft": [SqlDraft(sql="DELETE FROM sf_ops.incidents", columns=["x"], purpose="bad"),
                     SqlDraft(sql="SELECT station_id, sum(downtime_min) AS d FROM sf_ops.incidents GROUP BY station_id",
                              columns=["station_id", "d"], purpose="downtime by station")],
        "AnswerDraft": AnswerDraft(summary="F10 loses the most time.", recommendation="Check F10 supply.", findings=[
            Finding(claim="F10 lost 2205.5 minutes of downtime.", evidence=["E1"]),
            Finding(claim="F10 lost 9999 minutes.", evidence=["E1"]),
            Finding(claim="Shortages caused it.", evidence=["E7"])]),
    })
    sent = []
    result = Investigator(lambda sql: sent.append(sql) or [["F10", 2205.5], ["F30", 900.0]], llm, ask_id="ask-t").run(
        "Why does F10 keep stopping?")
    sql_ev = result["evidence"][0]
    assert sql_ev["ok"] and sql_ev["attempt"] == 2 and len(sent) == 1 and "DELETE" not in sent[0]
    assert result["evidence"][-1]["type"] == "logs"  # the coordinator always adds the log agent
    assert [f["claim"] for f in result["findings"]] == ["F10 lost 2205.5 minutes of downtime."]
    assert {d["claim"] for d in result["dropped"]} == {"F10 lost 9999 minutes.", "Shortages caused it."}
    steps = [json.loads(x)["step"] for x in (tmp_logs / "trace-ask-t.jsonl").read_text().splitlines()]
    assert steps[0] == "coordinator" and sorted(steps[1:3]) == ["logs", "sql"] and steps[3:] == ["analyst", "critic"]


def test_zetaris_error_is_fed_back_for_one_retry():
    llm = fake_llm({"Decomposition": Decomposition(tasks=[{"kind": "sql", "ask": "count incidents"}]),
                    "SqlDraft": [SqlDraft(sql="SELECT nope FROM sf_ops.incidents", columns=["n"], purpose="count"),
                                 SqlDraft(sql="SELECT count(*) FROM sf_ops.incidents", columns=["n"], purpose="count")],
                    "AnswerDraft": AnswerDraft(summary="ok ok", findings=[], recommendation="none")})

    def run_sql(sql):
        if "nope" in sql:
            raise RuntimeError("cannot resolve column nope")
        return [[42]]
    ev = Investigator(run_sql, llm, ask_id="ask-r").run("How many incidents?")["evidence"][0]
    assert ev["ok"] and ev["attempt"] == 2 and ev["preview"] == [[42]]


def test_without_a_model_the_rule_based_team_still_answers_from_offline_data():
    result = Investigator(offline_run_sql, None, ask_id="ask-o").run("Which AGV is least reliable and when?")
    assert result["coordinator"] == "rule-based" and result["findings"]
    assert "AGV-" in result["findings"][0]["claim"] and not result["dropped"]


# ---------------------------------------------------------------- custom incidents

def test_unsupported_kind_is_fenced_and_escalated(monkeypatch):
    from app import orchestrator
    from app.api import run_request
    monkeypatch.setattr(orchestrator, "SUPPORTED_KINDS", orchestrator.SUPPORTED_KINDS - {"near_miss"})
    out = run_request({"input": {"kind": "near_miss", "station": "F20", "severity": "high", "note": "forklift and walker"},
                       "options": {"sample_mode": True}})
    assert out["status"] == "success"
    assert out["output"]["decision"] == "ESCALATE"


def test_server_ask_and_custom_run(monkeypatch):
    monkeypatch.setenv("SAMPLE_MODE", "true")
    from app import server
    app = server.App(offline=True, replay=True)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def post(path, body):
        return urllib.request.urlopen(urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                                             headers={"Content-Type": "application/json"}))
    try:
        with pytest.raises(urllib.error.HTTPError):
            post("/run", {"kind": "alien_invasion", "station": "F10"})
        assert not app.busy
        post("/ask", {"question": "Are overheat alarms real?"})
        for _ in range(400):
            if app.last_ask:
                break
            time.sleep(0.05)
        assert app.last_ask["evidence"] and json.load(urllib.request.urlopen(base + "/api/ask_last"))["question"]
    finally:
        httpd.shutdown()


def test_a_task_that_names_another_runs_after_it_and_sees_its_rows():
    seen = []

    def llm(lane, messages, schema, **kw):
        if schema is Decomposition:
            return SimpleNamespace(model="m", data=Decomposition(tasks=[
                {"kind": "sql", "ask": "rank AGVs by faults"},
                {"kind": "sql", "ask": "faults per shift for the AGV from task 1"}]))
        if schema is SqlDraft:
            seen.append(messages[-1]["content"])
            return SimpleNamespace(model="m", data=SqlDraft(sql="SELECT agv_id FROM sf_fleet.agvs", columns=["agv_id"],
                                                            purpose="agvs"))
        return SimpleNamespace(model="m", data=AnswerDraft(summary="fine.", findings=[], recommendation="none"))
    Investigator(lambda sql: [["AGV-3"]], llm, ask_id="ask-d").run("Which AGV is least reliable and when?")
    assert seen[0] == "rank AGVs by faults" and "AGV-3" in seen[1] and "depends on" in seen[1]


def test_evidence_critic_model_can_only_drop_claims():
    from app.investigate.investigator import ClaimCheck
    llm = fake_llm({"Decomposition": Decomposition(tasks=[{"kind": "sql", "ask": "downtime by station"}]),
                    "SqlDraft": SqlDraft(sql="SELECT station_id, sum(downtime_min) FROM sf_ops.incidents GROUP BY 1",
                                         columns=["station_id", "d"], purpose="downtime"),
                    "AnswerDraft": AnswerDraft(summary="F10 worst.", recommendation="check", findings=[
                        Finding(claim="F10 lost 2205.5 minutes.", evidence=["E1"]),
                        Finding(claim="F10 is worse than F30 at 900.0 minutes.", evidence=["E1"])]),
                    "ClaimCheck": ClaimCheck(verdicts=[{"index": 1, "supported": False, "reason": "overstated"},
                                                       {"index": 7, "supported": False, "reason": "no such claim"}])})
    r = Investigator(lambda sql: [["F10", 2205.5], ["F30", 900.0]], llm, ask_id="ask-c").run("Why does F10 stop?")
    assert [f["claim"] for f in r["findings"]] == ["F10 lost 2205.5 minutes."]
    assert r["dropped"][0]["reason"].startswith("evidence critic model")


# ---------------------------------------------------------------- more than SQL: the floor and H-MEM

def test_floor_agent_adds_world_model_facts_for_what_the_question_names(tmp_logs):
    from app.sim.offline import offline_plant
    plant = offline_plant()
    result = Investigator(offline_run_sql, None, ask_id="ask-f", plant=plant).run("Why does F10 keep stopping?")
    floor = [e for e in result["evidence"] if e["type"] == "floor"]
    assert len(floor) == 1 and floor[0]["ok"] and floor[0]["columns"] == ["entity", "fact", "value"]
    facts = {(r[0], r[1]) for r in floor[0]["preview"]}
    assert ("F10", "name") in facts and ("F10", "line-side buffer (min)") in facts
    assert Investigator(offline_run_sql, None, plant=plant).floor_task("How many incidents last week?", 9) is None
    agvs = Investigator(offline_run_sql, None, plant=plant).floor_task("Which AGV is least reliable?", 3)
    assert {r[1] for r in agvs["preview"]} >= {"faults in the last 90 days", "battery health (%)"}


def test_log_agent_ranks_h_mem_lessons_by_the_question(tmp_logs):
    from app.engines.playbooks import PlaybookStore
    store = PlaybookStore(path=None)
    store.hmem.add("overheat at F20: robot cell entry needs lockout before anyone goes in", "SIM-1")
    store.hmem.add("part_shortage at F10: the kit cart was late twice on shift B", "SIM-2")
    ev = Investigator(offline_run_sql, None, memory=store).logs_task("Why does F10 run short of parts?", 1)
    assert ev["summary"]["h_mem_lessons"] and "F10" in ev["summary"]["h_mem_lessons"][0]
