"""Model registry: role -> chain from config, per-model breakers, fallback, requested vs answering model, receipts."""

import json
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from app import models
from app.llm import LLMUnavailable


REAL_RECEIPT_FOR = models.receipt_for  # captured before the autouse stub


class Out(BaseModel):
    ok: bool


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(models, "BREAKERS", {})
    monkeypatch.setattr(models, "LAST", {})
    monkeypatch.setattr(models, "receipt_for", lambda rid, wait_s=1.0: f"rcpt_{rid}" if rid else None)


def fake_complete(down=(), answer_as=None, calls=None):
    def complete(model, role, messages, max_tokens, timeout_s, json_mode):
        if calls is not None:
            calls.append((model, role, max_tokens, timeout_s))
        if model in down:
            raise LLMUnavailable("timeout")
        return SimpleNamespace(text='{"ok": true}', model=answer_as or model, requested=model, latency_s=0.1,
                               truncated=False, request_id=f"req-{len(calls or [])}", tokens=None)
    return complete


def test_every_role_has_an_nvidia_chain_and_a_reason():
    cfg = models.load_config()
    assert {"router", "monitor", "planner", "coordinator", "sql_agent", "analyst", "evidence_critic", "lab"} <= set(cfg["roles"])
    for role, spec in cfg["roles"].items():
        assert all(m.startswith("nvidia/") for m in spec["chain"]) and spec["why"] and spec["budget_s"] > 0
    assert cfg["roles"]["evidence_critic"]["chain"][0] != cfg["roles"]["analyst"]["chain"][0]  # independent check


def test_role_picks_its_chain_with_budget_and_receipt(monkeypatch):
    calls = []
    monkeypatch.setattr(models, "_complete", fake_complete(calls=calls))
    r = models.chat_json("fast", [{"role": "user", "content": "x"}], Out, role="sql_agent", timeout_s=999)
    spec = models.CFG["roles"]["sql_agent"]
    assert calls[0] == (spec["chain"][0], "sql_agent", spec["max_tokens"], spec["budget_s"])  # budget caps the timeout
    assert r.data.ok and r.requested == r.model == spec["chain"][0] and r.receipt == "rcpt_req-1" and not r.fell_back


def test_fallback_to_the_next_model_and_breaker_skips_it(monkeypatch):
    chain = models.CFG["roles"]["planner"]["chain"]
    calls = []
    monkeypatch.setattr(models, "_complete", fake_complete(down={chain[0]}, calls=calls))
    for _ in range(3):
        r = models.chat_json("deep", [{"role": "user", "content": "x"}], Out)  # deep lane -> planner role
        assert r.model == chain[1] and r.fell_back
    assert models.BREAKERS[chain[0]].state == "OPEN"
    assert [c[0] for c in calls].count(chain[0]) == 2  # threshold 2: then skipped without waiting
    assert r.tried[0] == {"model": chain[0], "skipped": "breaker open"}


def test_all_models_down_means_unavailable(monkeypatch):
    monkeypatch.setattr(models, "_complete", fake_complete(down=set(models.CFG["roles"]["monitor"]["chain"])))
    with pytest.raises(LLMUnavailable):
        models.chat("fast", [{"role": "user", "content": "x"}], role="monitor")


def test_silent_nightshift_fallback_is_recorded(monkeypatch):
    monkeypatch.setattr(models, "_complete", fake_complete(answer_as="nvidia/nemotron-3.5-super-vl-120b-a12b"))
    r = models.chat_json("fast", [{"role": "user", "content": "x"}], Out, role="router")
    assert r.requested != r.model and r.fell_back
    assert models.LAST["router"]["answering"] == "nvidia/nemotron-3.5-super-vl-120b-a12b"
    assert models.call_info(r)["receipt"].startswith("rcpt_")


def test_unknown_role_and_bad_config_are_refused(tmp_path):
    with pytest.raises(ValueError):
        models.role_for("deep", "poet")
    bad = tmp_path / "m.json"
    bad.write_text(json.dumps({"defaults": {}, "roles": {"planner": {"chain": ["no-provider"]}}}))
    with pytest.raises(ValueError):
        models.load_config(bad)


def test_receipt_is_matched_by_request_id(tmp_path, monkeypatch):
    from app import powered
    f = tmp_path / "receipts.jsonl"
    f.write_text("\n".join(json.dumps({"body": {"receipt": {"request_id": rid, "receipt_id": f"rcpt_{n}"}}})
                           for n, rid in enumerate(["chatcmpl-a", "chatcmpl-b"])) + "\n")
    monkeypatch.setattr(powered, "receipts_path", lambda: f)
    assert REAL_RECEIPT_FOR("chatcmpl-b", wait_s=0) == "rcpt_1" and REAL_RECEIPT_FOR("chatcmpl-z", wait_s=0) is None


def test_mapping_lists_roles_for_the_ui():
    rows = models.mapping()
    assert {r["role"] for r in rows} >= {"router", "planner", "sql_agent"} and all("why" in r for r in rows)
