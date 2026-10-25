"""Resilience primitives for model calls. No model calls here; app/models.py uses them.

- `backoff_delay`: exponential backoff with full jitter (AWS-style): uniform(0, min(cap, base * 2**attempt)).
- `AIMDLimiter`: adaptive concurrency per model. The limit grows by 1/limit per success and halves on an error or a
  very slow reply (additive increase, multiplicative decrease, as in TCP), so a struggling endpoint gets less load.
- `LatencyStats`: rolling latencies per role; the median sets when a hedged request fires.
- `Deadline`: an absolute time budget passed down from the incident to every call.
- `repair_json`: fixes the usual model JSON slips before we spend a re-ask.
"""

from __future__ import annotations

import json
import random
import re
import threading
import time
from collections import deque
from contextlib import contextmanager


def backoff_delay(attempt: int, base_s: float = 0.4, cap_s: float = 4.0, rng: random.Random | None = None) -> float:
    return (rng or random).uniform(0, min(cap_s, base_s * 2 ** attempt))


class LimiterTimeout(TimeoutError):
    """No concurrency slot became free before the deadline. Our own queueing, not a model failure: it must not
    trip a breaker or shrink the limit further (that spiral took both Nemotron lanes down under autopilot load)."""
    not_a_failure = True


class AIMDLimiter:
    def __init__(self, start: float = 3.0, low: float = 1.0, high: float = 8.0, slow_s: float | None = None):
        self.limit, self.low, self.high, self.slow_s = start, low, high, slow_s
        self.in_flight = 0
        self.cond = threading.Condition()

    @contextmanager
    def slot(self, timeout_s: float):
        end = time.monotonic() + max(0.0, timeout_s)
        with self.cond:
            while self.in_flight >= int(self.limit):
                left = end - time.monotonic()
                if left <= 0:
                    raise LimiterTimeout(f"no slot within {timeout_s:.1f} s (limit {int(self.limit)})")
                self.cond.wait(left)
            self.in_flight += 1
        try:
            yield
        finally:
            with self.cond:
                self.in_flight -= 1
                self.cond.notify()

    def success(self, latency_s: float) -> None:
        with self.cond:
            if self.slow_s is not None and latency_s > self.slow_s:
                self.limit = max(self.low, self.limit / 2)  # slow is a congestion signal too
            else:
                self.limit = min(self.high, self.limit + 1.0 / self.limit)
            self.cond.notify_all()

    def failure(self) -> None:
        with self.cond:
            self.limit = max(self.low, self.limit / 2)

    def state(self) -> dict:
        return {"limit": round(self.limit, 2), "in_flight": self.in_flight}


class LatencyStats:
    def __init__(self, size: int = 30):
        self.values: dict[str, deque] = {}
        self.lock = threading.Lock()
        self.size = size

    def add(self, key: str, seconds: float) -> None:
        with self.lock:
            self.values.setdefault(key, deque(maxlen=self.size)).append(seconds)

    def p50(self, key: str, default: float) -> float:
        with self.lock:
            v = sorted(self.values.get(key) or [])
        return v[len(v) // 2] if len(v) >= 3 else default


class DeadlineExceeded(TimeoutError):
    """The incident's time budget is spent: answer with the safe fallback instead."""


class Deadline:
    def __init__(self, seconds: float | None = None, at: float | None = None):
        self.at = at if at is not None else (time.monotonic() + seconds if seconds is not None else None)

    def left(self) -> float:
        return float("inf") if self.at is None else self.at - time.monotonic()

    def cap(self, timeout_s: float) -> float:
        left = self.left()
        if left <= 0.5:
            raise DeadlineExceeded("the incident's time budget is spent")
        return min(timeout_s, left)


def repair_json(text: str) -> str:
    """Best-effort repair: strip fences and prose, Python literals, trailing commas, single quotes, unclosed braces."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", t)
    start = t.find("{")
    if start == -1:
        return t
    t = t[start:]
    depth, end, in_str, esc = 0, None, False, False
    for i, ch in enumerate(t):  # cut at the matching closing brace (drops trailing prose)
        if in_str:
            esc = (ch == "\\" and not esc)
            if ch == '"' and not esc:
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end is not None:
        t = t[:end]
    if '"' not in t and "'" in t:
        t = t.replace("'", '"')
    t = re.sub(r"\bTrue\b", "true", re.sub(r"\bFalse\b", "false", re.sub(r"\bNone\b", "null", t)))
    t = re.sub(r",\s*([}\]])", r"\1", t)
    if end is None:  # truncated: close what is open
        opens = []
        in_str = esc = False
        for ch in t:
            if in_str:
                esc = (ch == "\\" and not esc)
                if ch == '"' and not esc:
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch in "{[":
                opens.append("}" if ch == "{" else "]")
            elif ch in "}]" and opens:
                opens.pop()
        t = t + ('"' if in_str else "") + "".join(reversed(opens))
        t = re.sub(r",\s*([}\]])", r"\1", t)
    json.loads(t)  # raise if still broken
    return t
