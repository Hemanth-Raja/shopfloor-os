"""Small TTL caches with hit/miss counters, shown in Health. No model calls.

- `zetaris`: read-only Zetaris results (SELECT/WITH), 120 s; any write through the same path clears it, so the agents
  never read their own stale state after a change.
- `history`: the investigator's incident history per (kind, station, AGV, 10-minute bucket), 300 s.
- `clauses`: H-MEM clause retrieval per (kind, query) and (rule, detail); the index only changes on restart.
- `llm_replay`: SAMPLE_MODE replies served from recordings (hits) or not recorded (misses).
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Hashable

_ALL: dict[str, "TTLCache"] = {}


class TTLCache:
    def __init__(self, name: str, ttl_s: float, max_items: int = 512, clock: Callable[[], float] = time.monotonic):
        self.name, self.ttl_s, self.max_items, self.clock = name, ttl_s, max_items, clock
        self.items: dict[Hashable, tuple[float, Any]] = {}
        self.hits = self.misses = self.invalidations = 0
        self._lock = threading.Lock()
        _ALL[name] = self

    def get(self, key: Hashable, compute: Callable[[], Any]) -> Any:
        with self._lock:
            hit = self.items.get(key)
            if hit and hit[0] > self.clock():
                self.hits += 1
                return hit[1]
            self.misses += 1
        value = compute()  # outside the lock: may be slow (Zetaris)
        with self._lock:
            if len(self.items) >= self.max_items:
                self.items.pop(next(iter(self.items)))
            self.items[key] = (self.clock() + self.ttl_s, value)
        return value

    def count(self, hit: bool) -> None:
        with self._lock:
            if hit:
                self.hits += 1
            else:
                self.misses += 1

    def clear(self) -> None:
        with self._lock:
            self.items.clear()
            self.invalidations += 1

    def stats(self) -> dict:
        total = self.hits + self.misses
        return {"hits": self.hits, "misses": self.misses, "hit_rate": round(self.hits / total, 2) if total else None,
                "items": len(self.items), "ttl_s": self.ttl_s, "invalidations": self.invalidations}


def cache(name: str, ttl_s: float, **kw) -> TTLCache:
    return _ALL.get(name) or TTLCache(name, ttl_s, **kw)


def stats() -> dict:
    return {name: c.stats() for name, c in sorted(_ALL.items())}


def cached_sql(run_sql: Callable, ttl_s: float = 120.0, name: str = "zetaris") -> Callable:
    """Wrap a run_sql: read-only statements are cached; anything else runs and clears the cache."""
    c = cache(name, ttl_s)

    def run(sql: str, *args, **kwargs):
        head = sql.lstrip().split(None, 1)[0].lower() if sql.strip() else ""
        if head in ("select", "with") and not args and not kwargs:
            return c.get(" ".join(sql.split()), lambda: run_sql(sql))
        out = run_sql(sql, *args, **kwargs)
        c.clear()  # a write changed the world: no stale reads
        return out
    return run
