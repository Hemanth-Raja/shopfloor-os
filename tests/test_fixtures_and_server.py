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
    with pytest.raises(LLMUnavailable):
        maybe_sample(None, "json", mode="true", folder=tmp_path)("deep", [{"role": "user", "content": "new"}], Plan)


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
        assert trace.decision.decision == "ACT" and trace.critic_rejections == 1
        assert "<canvas" in urllib.request.urlopen(base + "/").read().decode()
        assert json.load(urllib.request.urlopen(base + "/health"))["floor"]["collisions"] == 0
    finally:
        httpd.shutdown()
