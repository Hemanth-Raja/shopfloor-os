"""SAMPLE_MODE record/replay and the stdlib web server (offline, replaying the committed fixtures)."""

import json
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from app.contracts import Plan
from app.fixtures import maybe_sample
from app.llm import LLMUnavailable


def fake_json(lane, messages, schema, **kw):
    return SimpleNamespace(data=Plan(incident_id="i", assignee_ids=["w"], priority="low", rationale="r"),
                           text="{}", model="m", latency_s=1.0)


def test_record_then_replay_without_the_model(tmp_path):
    msgs = [{"role": "user", "content": "plan please"}]
    recorded = maybe_sample(fake_json, "json", mode="record", folder=tmp_path)("deep", msgs, Plan)
    replayed = maybe_sample(None, "json", mode="true", folder=tmp_path)("deep", msgs, Plan)
    assert replayed.data == recorded.data and replayed.model == "m (replay)"
    from app.lab.experiment import Hypothesis
    with pytest.raises(LLMUnavailable):  # no recording and no rule-based stand-in for this schema
        maybe_sample(None, "json", mode="true", folder=tmp_path)("deep", [{"role": "user", "content": "new"}], Hypothesis)


def test_unrecorded_plan_uses_the_rule_based_planner(tmp_path):
    payload = {"incident": {"id": "SIM-9-001", "kind": "part_shortage", "station_id": "T20"},
               "worker_candidates": [{"id": "worker:plant:W-18"}], "agv_candidates": [{"id": "agv:plant:AGV-2"}],
               "relevant_clauses": [{"ref": "29 CFR 1910.178(n)(4)", "text": "slow down"}]}
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": json.dumps(payload)}]
    reply = maybe_sample(None, "json", mode="true", folder=tmp_path)("deep", msgs, Plan)
    plan = reply.data
    assert reply.model.startswith("rule-based") and plan.assignee_ids == ["worker:plant:W-18"]
    assert plan.agv_routes[0].crossing_speed_mps == 0.5 and plan.citations == ["29 CFR 1910.178(n)(4)"]


def test_passthrough_when_sample_mode_is_off():
    assert maybe_sample(fake_json, "json", mode="false") is fake_json


def test_server_runs_a_demo_offline(monkeypatch):
    monkeypatch.setenv("SAMPLE_MODE", "true")
    from app import server
    app = server.App(offline=True, replay=True)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        state = json.load(urllib.request.urlopen(base + "/api/state"))
        assert state["grid"]["cols"] == 40 and len(state["agents"]) > 8
        req = urllib.request.Request(base + "/run", data=json.dumps({"kind": "part_shortage", "station": "F30"}).encode(),
                                     headers={"Content-Type": "application/json"})
        inc = json.load(urllib.request.urlopen(req))["incident_id"]
        for _ in range(100):
            if inc in app.traces:
                break
            time.sleep(0.05)
        trace = app.traces[inc]
        assert trace.decision.decision == "ACT" and trace.critic_rejections in (0, 1)  # v2 may or may not break a rule
        assert "<canvas" in urllib.request.urlopen(base + "/").read().decode()
        assert json.load(urllib.request.urlopen(base + "/health"))["floor"]["collisions"] == 0
    finally:
        httpd.shutdown()


def test_knowledge_and_offline_lab(monkeypatch, tmp_path):
    monkeypatch.setenv("SAMPLE_MODE", "true")
    from app import server
    from app.engines.playbooks import PlaybookStore
    app = server.App(offline=True, replay=True)
    k = app.knowledge()
    assert k["engine"] == "Meterless World Model" and k["counts"]["agv"] == 8 and k["memory"]["lessons"] == []
    assert any(a["id"] == "AGV-3" and a["service_overdue"] for a in k["agvs"])
    assert any(r["id"] == "R-LOTO" for r in k["rules"])
    app.memory = PlaybookStore(tmp_path / "pb.json")
    app.memory.hmem.add("part_shortage at F30: R-CROSSING slow down", source="SIM-1", tags=["kind:part_shortage"])
    assert app.knowledge()["memory"]["lessons"][0]["source"] == "SIM-1"
    got = []
    app.bus.publish = lambda kind, data: got.append((kind, data))
    app.lab()
    for _ in range(200):
        if any(k == "lab" for k, _ in got):
            break
        time.sleep(0.05)
    result = next(d for k, d in got if k == "lab")
    assert "error" not in result and result["replays"] and result["ci_low_pct"] <= result["effect_pct"] <= result["ci_high_pct"]


def test_server_starts_degraded_when_zetaris_is_down_at_start(monkeypatch):
    """Chaos test finding: Zetaris down at start used to crash the server."""
    from app import server
    import app.engines.plant_world as pw

    def down(*a, **k):
        raise ConnectionError("Zetaris unreachable")
    monkeypatch.setattr(pw, "load_plant_world", down)
    from app import models
    monkeypatch.setattr(models, "self_check", lambda *a, **k: {})  # no network in tests
    app = server.App(offline=False, replay=False)
    assert app.offline and app.degraded and app.plant.entities("worker")


def test_plant_refresh_swaps_the_snapshot_and_keeps_the_last_good_one_on_failure(monkeypatch):
    monkeypatch.setenv("SAMPLE_MODE", "true")
    from app import server
    import app.engines.plant_world as pw
    from app.sim.offline import offline_plant
    app = server.App(offline=True, replay=True)
    fresh = offline_plant()
    fresh.version = "v2"
    monkeypatch.setattr(pw, "load_plant_world", lambda *a, **k: fresh)
    assert app.refresh_plant() == {"ok": True, "version": "v2", "changed": True} and app.plant is fresh

    def down(*a, **k):
        raise ConnectionError("Zetaris unreachable")
    monkeypatch.setattr(pw, "load_plant_world", down)
    out = app.refresh_plant()
    assert not out["ok"] and app.plant is fresh and app.health()["plant"]["stale"]


def test_replay_runs_get_their_own_ids_and_still_use_the_recorded_replies(monkeypatch):
    monkeypatch.setenv("SAMPLE_MODE", "true")
    from app import server
    app = server.App(offline=True, replay=True)
    ids = []
    for kind, station in (("part_shortage", "F30"), ("part_shortage", "F30")):
        ids.append(app.run({"kind": kind, "station": station}))
        for _ in range(200):
            if ids[-1] in app.traces:
                break
            time.sleep(0.05)
    assert ids == ["SIM-42-001", "SIM-42-002"]
    assert {r["incident"] for r in app.incidents()} == set(ids)
    monitor = [s for s in app.traces["SIM-42-002"].steps if s.step == "monitor"][0]
    assert "(replay)" in monitor.detail["model_call"]["answering"]  # fixture recorded for -001 still matches
    assert app.incident_history("SIM-42-001")["steps"]


def test_incidents_you_run_work_at_the_same_time(monkeypatch):
    monkeypatch.setenv("SAMPLE_MODE", "true")
    import time
    from app import server
    app = server.App(offline=True, replay=True)
    a = app.run({"kind": "part_shortage", "station": "F30"})
    b = app.run({"kind": "spill", "station": "F20"})  # no "an incident is already running"
    c = app.run({"kind": "overheat", "station": "F10", "false_alarm": True})
    assert len({a, b, c}) == 3
    for _ in range(400):
        if all(i in app.traces for i in (a, b, c)) and app.floor.idle():
            break
        time.sleep(0.05)
    assert all(i in app.traces for i in (a, b, c)) and app.floor.idle()
    assert {app.trace_meta[i]["source"] for i in (a, b, c)} == {"you ran it"}
    assert app.floor.max_active >= 2  # the second and third started while the first was still working
    app.floor.queue.append((0, 0, app.traces[a].incident, 0, None))  # something still open
    with pytest.raises(RuntimeError, match="runs alone"):
        app.run({"kind": "spill", "station": "F20", "mode": "linear"})
