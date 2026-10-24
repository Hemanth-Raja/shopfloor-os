"""Circuit breaker states, fast safe mode when Nightshift is down, and the health report."""

import pytest

from app.agents.health import CircuitBreaker, CircuitOpen, check_health
from app.llm import LLMUnavailable
from app.orchestrator import Orchestrator

from conftest import FakePlanner


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def boom():
    raise TimeoutError("nightshift timed out")


def test_breaker_opens_half_opens_and_closes():
    clock = Clock()
    b = CircuitBreaker("nightshift", threshold=3, cooldown_s=60, clock=clock)
    for _ in range(3):
        with pytest.raises(LLMUnavailable):
            b.call(boom)
    assert b.state == "OPEN"
    with pytest.raises(CircuitOpen):
        b.call(lambda: "never called")
    clock.t = 61
    assert b.state == "HALF_OPEN"
    assert b.call(lambda: "ok") == "ok" and b.state == "CLOSED" and b.failures == 0


def test_half_open_failure_reopens():
    clock = Clock()
    b = CircuitBreaker("zetaris", threshold=1, cooldown_s=10, clock=clock)
    with pytest.raises(TimeoutError):
        b.call(boom)
    clock.t = 11
    with pytest.raises(TimeoutError):
        b.call(boom)
    assert b.state == "OPEN"


def test_open_breaker_gives_instant_safe_mode(sim, plant, tmp_path):
    b = CircuitBreaker("nightshift", threshold=1)
    with pytest.raises(LLMUnavailable):
        b.call(boom)
    llm = FakePlanner()
    trace = Orchestrator(plant, sim, b.wrap(llm), trace_path=None).handle(sim.inject("spill", plant, station_id="F20"))
    assert trace.safe_mode and trace.decision.decision == "ESCALATE" and llm.calls == 0


def test_health_report(sim):
    breakers = {"nightshift": CircuitBreaker("nightshift", threshold=1), "zetaris": CircuitBreaker("zetaris")}
    report = check_health(breakers, {"zetaris": lambda: [["1"]], "nightshift": boom}, sim)
    assert report["zetaris"]["status"] == "ok"
    assert report["nightshift"]["status"] == "down" and report["safe_mode"] is True
    assert report["floor"]["collisions"] == 0 and report["overall"] == "down"
