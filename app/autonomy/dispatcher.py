"""Dispatcher: a priority queue of incidents and N concurrent orchestrators, each taking its own dynamic route.

Priority is risk to people first, then severity, then the expected cost of waiting (historical cost per kind
from sf_ops.incidents). A new, more urgent incident is admitted at once even when every slot is busy, and it can
pre-empt the resources of a less urgent one (app/autonomy/leases.py).

Two ways to run, same logic:
- cooperative (`threads=False`): one thread interleaves the incidents hop by hop, most urgent first. Deterministic
  for a seed, so SAMPLE_MODE and the tests replay exactly.
- threaded (`threads=True`): each active incident runs in its own thread, so live model calls overlap; hops that
  touch the floor take the floor lock.
"""

from __future__ import annotations

import heapq
import itertools
import threading
import time
from typing import Callable

from app.agents.restraint import STAT_KIND
from app.orchestrator import PERSON_HAZARD_KINDS, SIGN_OFF_KINDS

SEVERITY = {"low": 0, "medium": 1, "high": 2, "critical": 3}
PEOPLE = PERSON_HAZARD_KINDS | SIGN_OFF_KINDS  # a person is (or will be) in the hazard


def incident_risk(incident, stats: dict) -> float:
    """People first (100), then severity (10 per level), then expected cost (up to 9.9)."""
    cost = (stats.get(STAT_KIND.get(incident.kind, incident.kind)) or {}).get("avg_cost_usd") or 5000.0
    return round((100 if incident.kind in PEOPLE else 0) + 10 * SEVERITY.get(incident.severity, 1)
                 + min(cost / 2000.0, 9.9), 2)


class Dispatcher:
    def __init__(self, make_orchestrator: Callable, stats: dict, workers: int = 3, threads: bool = False,
                 on_event: Callable | None = None, on_done: Callable | None = None, leases=None, sim=None):
        self.make, self.stats, self.workers, self.threads = make_orchestrator, stats, workers, threads
        self.on_event, self.on_done = on_event or (lambda kind, data: None), on_done
        self.leases, self.sim = leases, sim
        self.queue: list[tuple] = []
        self.seq = itertools.count()
        self.active: dict[str, dict] = {}
        self.done: list[dict] = []
        self.max_active = 0
        self.lock = threading.RLock()

    # ---------------------------------------------------------------- queue

    def submit(self, incident, detected_tick: int = 0, stall=None) -> float:
        risk = incident_risk(incident, self.stats)
        with self.lock:
            heapq.heappush(self.queue, (-risk, next(self.seq), incident, detected_tick, stall))
        self.on_event("queued", {"incident": incident.id, "kind": incident.kind, "risk": risk,
                                 "station": incident.station_id})
        return risk

    def queued(self) -> list[dict]:
        with self.lock:
            return [{"incident": i.id, "kind": i.kind, "station": i.station_id, "risk": -r}
                    for r, _, i, _, _ in sorted(self.queue)]

    def _admit(self) -> list[dict]:
        """Start the most urgent queued incidents: up to `workers`, plus any that out-rank every active one."""
        started = []
        with self.lock:
            while self.queue:
                risk = -self.queue[0][0]
                lowest = min((a["risk"] for a in self.active.values()), default=None)
                if len(self.active) >= self.workers and not (lowest is not None and risk >= lowest + 50):
                    break  # full, and not urgent enough to jump in (people at risk vs. none)
                _, _, inc, tick, stall = heapq.heappop(self.queue)
                orch = self.make(inc, risk)
                entry = {"incident": inc, "risk": risk, "orch": orch, "gen": orch.run_steps(inc, stall),
                         "detected_tick": tick, "started": time.perf_counter(), "hops": 0}
                self.active[inc.id] = entry
                self.max_active = max(self.max_active, len(self.active))
                started.append(entry)
        for e in started:
            self.on_event("started", {"incident": e["incident"].id, "risk": e["risk"]})
            if self.threads:
                threading.Thread(target=self._run_thread, args=(e,), daemon=True).start()
        return started

    # ---------------------------------------------------------------- running

    def _finish(self, entry: dict, trace) -> None:
        inc = entry["incident"]
        if self.leases is not None and (trace is None or trace.gate != "awaiting_approval"):
            self.leases.release_all(inc.id)  # a plan waiting for a person keeps its resources (until the lease expires)
        result = {"incident": inc.id, "kind": inc.kind, "station": inc.station_id, "risk": entry["risk"],
                  "decision": trace.decision.decision if trace and trace.decision else None,
                  "gate": trace.gate if trace else None, "hops": len(trace.path) if trace else 0,
                  "route": [p["agent"] for p in trace.path] if trace else [],
                  "detected_tick": entry["detected_tick"], "finished_tick": self.sim.tick if self.sim else None,
                  "replans": trace.replans if trace else 0, "seconds": round(time.perf_counter() - entry["started"], 2)}
        with self.lock:
            self.active.pop(inc.id, None)
            self.done.append(result)
        if self.on_done:
            self.on_done(trace, result)
        self.on_event("finished", result)

    def _run_thread(self, entry: dict) -> None:
        trace = None
        try:
            while True:
                next(entry["gen"])
                entry["hops"] += 1
        except StopIteration as stop:
            trace = stop.value
        except Exception as err:  # noqa: BLE001 - one incident failing never stops the shift
            self.on_event("error", {"incident": entry["incident"].id, "error": f"{type(err).__name__}: {err}"[:300]})
        self._finish(entry, trace)

    def step(self) -> int:
        """Cooperative mode: admit, then advance every active incident by one hop, most urgent first."""
        self._admit()
        if self.threads:
            return len(self.active)
        for entry in sorted(list(self.active.values()), key=lambda e: -e["risk"]):
            try:
                next(entry["gen"])
                entry["hops"] += 1
            except StopIteration as stop:
                self._finish(entry, stop.value)
            except Exception as err:  # noqa: BLE001
                self.on_event("error", {"incident": entry["incident"].id, "error": f"{type(err).__name__}: {err}"[:300]})
                self._finish(entry, None)
        return len(self.active)

    def idle(self) -> bool:
        with self.lock:
            return not self.queue and not self.active
