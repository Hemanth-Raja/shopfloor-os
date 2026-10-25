"""Health agent and circuit breakers.

A circuit breaker wraps each external dependency (Nightshift, Zetaris):
- After `threshold` consecutive failures it OPENS, and calls fail at once without waiting for a timeout.
- After `cooldown_s` it lets one trial call through (HALF_OPEN). Success closes it; failure opens it again.

The model breaker raises LLMUnavailable, so the orchestrator drops straight into safe mode: a rule-based
fallback plan plus ESCALATE. The health agent reports every dependency and the floor's own vital signs.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from app.llm import LLMUnavailable


class CircuitOpen(LLMUnavailable):
    """Raised instead of calling a dependency whose breaker is open."""


class CircuitBreaker:
    def __init__(self, name: str, threshold: int = 3, cooldown_s: float = 60.0, clock: Callable[[], float] = time.monotonic):
        self.name, self.threshold, self.cooldown_s, self.clock = name, threshold, cooldown_s, clock
        self.failures = 0
        self.opened_at: float | None = None
        self.last_error = ""
        self._probing = False  # half-open: exactly one trial call at a time

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "CLOSED"
        return "HALF_OPEN" if self.clock() - self.opened_at >= self.cooldown_s else "OPEN"

    def call(self, fn: Callable, *args, **kwargs) -> Any:
        state = self.state
        if state == "OPEN" or (state == "HALF_OPEN" and self._probing):
            raise CircuitOpen(f"{self.name} circuit open after {self.failures} failures: {self.last_error[:120]}")
        probe = state == "HALF_OPEN"
        if probe:
            self._probing = True
        try:
            out = fn(*args, **kwargs)
        except Exception as err:  # noqa: BLE001 - any failure counts against the dependency
            if probe:
                self._probing = False
            if getattr(err, "not_a_failure", False):  # our own pacing (model-call budget), not a broken dependency
                raise
            self.failures += 1
            self.last_error = f"{type(err).__name__}: {err}"
            if self.failures >= self.threshold or self.state == "HALF_OPEN":
                self.opened_at = self.clock()
            if isinstance(err, LLMUnavailable) or self.name != "nightshift":
                raise
            raise LLMUnavailable(self.last_error) from err  # model failures always mean safe mode
        self.failures, self.opened_at, self._probing = 0, None, False
        return out

    def wrap(self, fn: Callable) -> Callable:
        return lambda *a, **k: self.call(fn, *a, **k)

    def status(self) -> dict:
        return {"state": self.state, "failures": self.failures, "last_error": self.last_error[:160]}


def check_health(breakers: dict[str, CircuitBreaker], probes: dict[str, Callable[[], Any]], sim=None) -> dict:
    """Probe each dependency through its breaker; add the floor's vital signs from the sim."""
    out: dict[str, Any] = {}
    for name, probe in probes.items():
        started = time.perf_counter()
        breaker = breakers.get(name)
        try:
            (breaker.call(probe) if breaker else probe())
            status = "ok"
        except Exception as err:  # noqa: BLE001
            status = "down" if breaker is None or breaker.state != "CLOSED" else "degraded"
            out.setdefault("errors", {})[name] = f"{type(err).__name__}: {str(err)[:120]}"
        out[name] = {"status": status, "ms": round((time.perf_counter() - started) * 1000, 1),
                     **({"breaker": breaker.status()} if breaker else {})}
    if sim is not None:
        agvs = [a for a in sim.agents.values() if a.kind == "agv"]
        out["floor"] = {"status": "ok" if not sim.collisions else "down", "tick": sim.tick,
                        "collisions": len(sim.collisions),
                        "agvs_down": sorted(a.id for a in agvs if a.status in ("fault", "stalled")),
                        "geofence_cells": len(sim.geofence)}
    states = [v["status"] for k, v in out.items() if isinstance(v, dict) and "status" in v]
    out["overall"] = "down" if "down" in states else "degraded" if "degraded" in states else "ok"
    out["safe_mode"] = any(b.state != "CLOSED" for b in breakers.values())
    return out
