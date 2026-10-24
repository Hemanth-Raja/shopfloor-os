"""Run a shift autonomously: signals arrive, the Sentinel raises incidents, the dispatcher runs them concurrently.

    python run.py shift --minutes 10 --seed 7            # headless, as fast as the agents allow
    SAMPLE_MODE=true python run.py shift --minutes 10    # offline and deterministic for the seed

The human gate still applies per the autonomy level: anything risky waits for a person, and the summary lists it.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from app.autonomy.dispatcher import Dispatcher
from app.autonomy.leases import LeaseBook
from app.autonomy.sentinel import TICKS_PER_MIN, Sentinel, generate_signals
from app.sim.sim import TICK_S

DRAIN_TICKS = 600  # after the shift ends, finish what is open for up to 20 sim minutes
LIVE_TICK_S = 0.5  # live (threaded) runs pace the floor clock like the UI: one 2 s tick per 0.5 s
DRAIN_WALL_S = 600  # live: wait at most 10 minutes for open incidents after the shift ends


class Shift:
    def __init__(self, plant, sim, make_orchestrator: Callable, minutes: float = 10, seed: int = 7, workers: int = 3,
                 threads: bool = False, sentinel_llm: Callable | None = None, on_event: Callable | None = None,
                 sim_lock=None, memory=None, rate_per_min: float = 1.0, journal=None):
        self.plant, self.sim, self.minutes, self.seed = plant, sim, minutes, seed
        self.journal = journal  # app/journal.py: durable, hash-chained record of the shift
        outer = on_event or (lambda kind, data: None)

        def on_event(kind, data):
            if self.journal is not None and kind in ("queued", "started", "finished"):
                self.journal.append(kind, {k: v for k, v in data.items() if k != "route"} | (
                    {"route": data["route"]} if "route" in data else {}))
            outer(kind, data)
        self.on_event = on_event
        self.sim_lock = sim_lock or threading.RLock()
        self.memory = memory
        self.leases = LeaseBook(ttl_s=300.0, clock=lambda: self.sim.tick * TICK_S)  # leases run on floor time
        if journal is not None:
            self.leases.on_event = journal.append
            self.leases.next_token = journal.recover()["last_token"] + 1  # fencing tokens never go backwards
        from app.connectors import SimFleet
        self.fleet = SimFleet(sim)
        self.signals = generate_signals(plant, minutes, seed, rate_per_min)
        self.start_tick = sim.tick
        self.sentinel = Sentinel(sentinel_llm, on_event=self.on_event)
        self.traces: dict[str, object] = {}
        self.dispatcher = Dispatcher(lambda inc, risk: self._with_fleet(make_orchestrator(inc, risk, self.leases,
                                                                                          self.sim_lock)),
                                     plant.stats, workers=workers, threads=threads, on_event=self.on_event,
                                     on_done=self._done, leases=self.leases, sim=sim)
        self.returns: dict[str, tuple] = {}  # responders to send back once they are free
        self.next_signal = 0
        self.stopped = False
        self.wall_started = time.perf_counter()

    def _with_fleet(self, orch):
        orch.fleet = self.fleet
        return orch

    def begin(self) -> None:
        if self.journal is not None:
            self.journal.append("shift_started", {"minutes": self.minutes, "seed": self.seed,
                                                  "signals": len(self.signals)})

    def _done(self, trace, result) -> None:
        self.sentinel.close(result["incident"])
        if trace is not None:
            self.traces[trace.incident.id] = trace
            if trace.gate == "auto_executed":
                for aid, cell in trace.dispatch.items():
                    self.returns[aid] = tuple(cell)
            if self.memory is not None:
                self.memory.record(trace)
        with self.sim_lock:
            self.sim.clear_geofence(result["incident"])

    def _raise(self, spec: dict):
        try:
            with self.sim_lock:
                agv = f"agv:plant:{spec['agv']}" if spec.get("agv") else None
                if agv and (agv not in self.sim.agents or self.sim.agents[agv].status == "fault"):
                    return None
                kind = spec["kind"]
                return self.sim.inject(kind, self.plant, station_id=spec["station"], agv_id=agv,
                                       false_alarm=spec.get("false_alarm"), severity=spec["severity"], note=spec["note"])
        except (KeyError, ValueError):
            return None

    def due(self) -> list[dict]:
        out = []
        now = self.sim.tick - self.start_tick
        while self.next_signal < len(self.signals) and self.signals[self.next_signal]["tick"] <= now:
            out.append(self.signals[self.next_signal])
            self.next_signal += 1
        return out

    def send_back(self) -> None:
        """Responders whose job is done walk back to where they came from, so busy stations do not fill up."""
        with self.sim_lock:
            for aid, home in list(self.returns.items()):
                agent = self.sim.agents.get(aid)
                if agent is None or agent.status in ("fault", "stalled", "held"):
                    self.returns.pop(aid)
                    continue
                if agent.status == "moving" or self.leases.holder(aid):
                    continue  # still on its way, or already working on another incident
                self.returns.pop(aid)
                if agent.pos != home:
                    self.sim.assign(aid, home, agent.speed_mps)

    def watch(self) -> None:
        """One Sentinel pass over the signals that arrived since the last pass."""
        for sig in self.due():
            inc = self.sentinel.process(sig, self.sim.tick, self._raise)
            if inc is not None:
                self.dispatcher.submit(inc, detected_tick=self.sim.tick)

    @property
    def shift_over(self) -> bool:
        return self.sim.tick - self.start_tick >= self.minutes * TICKS_PER_MIN

    def finished(self) -> bool:
        if self.stopped or (self.shift_over and self.next_signal >= len(self.signals) and self.dispatcher.idle()):
            return True
        if self.dispatcher.threads:  # live: model calls take seconds; wait for open incidents, up to a wall-clock cap
            return self.shift_over and time.perf_counter() - self.wall_started > \
                self.minutes * TICKS_PER_MIN * LIVE_TICK_S + DRAIN_WALL_S
        return self.sim.tick - self.start_tick > self.minutes * TICKS_PER_MIN + DRAIN_TICKS

    def run_headless(self) -> dict:
        """Drive the floor clock ourselves: one tick, one Sentinel pass, one dispatcher step, repeat."""
        self.begin()
        while not self.finished():
            with self.sim_lock:
                self.sim.step()
            self.send_back()
            self.watch()
            self.dispatcher.step()
            if self.dispatcher.threads:
                time.sleep(LIVE_TICK_S)
        return self.end()

    def end(self) -> dict:
        out = self.summary()
        if self.journal is not None:
            self.journal.append("shift_finished", {k: v for k, v in out.items() if k != "results"})
        return out

    def poll(self) -> None:
        """For a clock driven elsewhere (the web server's ticker): watch and dispatch once."""
        self.send_back()
        self.watch()
        self.dispatcher.step()

    def summary(self) -> dict:
        done = self.dispatcher.done
        by_decision: dict[str, int] = {}
        for r in done:
            by_decision[r["decision"] or "none"] = by_decision.get(r["decision"] or "none", 0) + 1
        resolved = [r for r in done if r["gate"] == "auto_executed" and r["finished_tick"] is not None]
        mins = [(r["finished_tick"] - r["detected_tick"]) * TICK_S / 60 for r in resolved]
        saved = 0.0
        for r in resolved:
            stat = self.plant.stats.get({"overheat": "machine_overheat", "spill": "fluid_spill"}.get(r["kind"], r["kind"])) or {}
            hist_min, cars = stat.get("avg_downtime_min") or 0, stat.get("avg_cars_lost") or 0
            ours = (r["finished_tick"] - r["detected_tick"]) * TICK_S / 60
            if hist_min:
                saved += cars * max(0.0, 1 - ours / hist_min)
        return {
            "minutes": self.minutes, "seed": self.seed, "signals": len(self.signals),
            "sentinel": dict(self.sentinel.stats), "incidents": len(done), "decisions": by_decision,
            "auto_resolved": len(resolved),
            "waiting_for_a_person": [r["incident"] for r in done if r["gate"] in ("awaiting_approval", "advisory")],
            "escalations": [r["incident"] for r in done if r["decision"] in ("ESCALATE", "SAFE_HOLD")],
            "mean_minutes_to_resolve": round(sum(mins) / len(mins), 1) if mins else None,
            "cars_saved_estimate": round(saved, 1),
            "preemptions": sum(1 for l in self.leases.log if l["op"] == "preempt"),
            "replans": sum(r["replans"] for r in done),
            "max_concurrent": self.dispatcher.max_active, "wall_seconds": round(time.perf_counter() - self.wall_started, 1),
            "unfinished": [e["incident"].id for e in list(self.dispatcher.active.values())]
            + [q["incident"] for q in self.dispatcher.queued()],
            "results": done,
        }
