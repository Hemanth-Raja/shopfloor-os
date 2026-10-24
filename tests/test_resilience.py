"""Resilient model calls: backoff, AIMD limiter, JSON repair, retries, hedging, deadlines, half-open probing."""

import random
import threading
import time
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from app import models
from app.agents.health import CircuitBreaker, CircuitOpen
from app.llm import LLMUnavailable
from app.resilience import AIMDLimiter, Deadline, LimiterTimeout, backoff_delay, repair_json


class Out(BaseModel):
    ok: bool
    n: int = 0


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(models, "BREAKERS", {})
    monkeypatch.setattr(models, "LIMITERS", {})
    monkeypatch.setattr(models, "LAST", {})
    monkeypatch.setattr(models, "SLEEP", lambda s: None)
    monkeypatch.setattr(models, "receipt_for", lambda rid, wait_s=1.0: None)


def reply(model, text='{"ok": true}', latency=0.1):
    return SimpleNamespace(text=text, model=model, requested=model, latency_s=latency, truncated=False,
                           request_id=None, tokens=None)


def test_full_jitter_backoff_stays_within_its_cap():
    rng = random.Random(1)
    for attempt in range(6):
        d = [backoff_delay(attempt, base_s=0.5, cap_s=3.0, rng=rng) for _ in range(200)]
        assert 0 <= min(d) and max(d) <= min(3.0, 0.5 * 2 ** attempt)


def test_aimd_halves_on_errors_and_grows_slowly_on_success():
    lim = AIMDLimiter(start=4, low=1, high=8, slow_s=10)
    lim.failure()
    assert lim.limit == 2
    lim.failure(); lim.failure()
    assert lim.limit == 1  # never below the floor
    for _ in range(3):
        lim.success(0.5)
    assert 1 < lim.limit < 3  # additive: about +1/limit per success
    lim.success(30)  # a very slow reply is congestion too
    assert lim.limit < 2
    with lim.slot(0.1):
        with pytest.raises(LimiterTimeout):
            with lim.slot(0.05):
                pass


@pytest.mark.parametrize("raw", ['```json\n{"ok": true}\n```', 'Sure! {"ok": True, "n": 2,} hope that helps',
                                 "{'ok': True}", '{"ok": true, "n": 3', '{"ok": false, "n": [1, 2'])
def test_json_repair_handles_common_slips(raw):
    import json
    assert "ok" in json.loads(repair_json(raw))


def test_empty_and_5xx_replies_are_retried_on_the_same_model(monkeypatch):
    chain = models.CFG["roles"]["planner"]["chain"]
    seq = iter([LLMUnavailable("HTTP 502"), LLMUnavailable("empty reply"), reply(chain[0])])

    def complete(model, role, messages, max_tokens, timeout_s, json_mode):
        r = next(seq)
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(models, "_complete", complete)
    r = models.chat_json("deep", [{"role": "user", "content": "x"}], Out, role="planner")
    assert r.model == chain[0] and r.retries == 2 and not r.fell_back
    assert models.BREAKERS[chain[0]].state == "CLOSED"  # retries inside one attempt do not trip the breaker


def test_a_broken_json_reply_is_repaired_without_a_reask(monkeypatch):
    calls = []

    def complete(model, role, messages, max_tokens, timeout_s, json_mode):
        calls.append(model)
        return reply(model, text="Here you go: {'ok': True,}")
    monkeypatch.setattr(models, "_complete", complete)
    r = models.chat_json("deep", [{"role": "user", "content": "x"}], Out, role="analyst")
    assert r.data.ok and r.repaired and len(calls) == 1


def test_hedged_request_takes_the_faster_model(monkeypatch):
    chain = models.CFG["roles"]["router"]["chain"]
    assert models.CFG["roles"]["router"].get("hedge")
    monkeypatch.setattr(models.LATENCY, "p50", lambda key, default: 0.05)

    def complete(model, role, messages, max_tokens, timeout_s, json_mode):
        time.sleep(1.0 if model == chain[0] else 0.01)  # the first model hangs
        return reply(model)
    monkeypatch.setattr(models, "_complete", complete)
    t = time.perf_counter()
    r = models.chat_json("fast", [{"role": "user", "content": "x"}], Out, role="router")
    assert r.model == chain[1] and time.perf_counter() - t < 0.8
    assert any("hedged_after_s" in x for x in r.tried)


def test_a_spent_deadline_fails_fast(monkeypatch):
    monkeypatch.setattr(models, "_complete", lambda *a, **k: pytest.fail("must not call the model"))
    t = time.perf_counter()
    with pytest.raises(LLMUnavailable):
        models.chat_json("deep", [{"role": "user", "content": "x"}], Out, role="planner", deadline=time.monotonic() + 0.2)
    assert time.perf_counter() - t < 0.5
    assert Deadline(seconds=10).cap(30) <= 10


def test_half_open_lets_exactly_one_probe_through():
    now = [0.0]
    b = CircuitBreaker("m", threshold=1, cooldown_s=5, clock=lambda: now[0])
    with pytest.raises(Exception):
        b.call(lambda: (_ for _ in ()).throw(RuntimeError("down")))
    now[0] = 6  # cooldown over: HALF_OPEN
    gate, results = threading.Event(), []

    def slow_probe():
        gate.wait(1)
        return "ok"
    t = threading.Thread(target=lambda: results.append(b.call(slow_probe)))
    t.start()
    time.sleep(0.05)
    with pytest.raises(CircuitOpen):  # a second caller during the probe is refused
        b.call(lambda: "other")
    gate.set(); t.join()
    assert results == ["ok"] and b.state == "CLOSED"
