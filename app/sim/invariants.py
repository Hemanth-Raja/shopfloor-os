"""Runtime invariant monitor: checks the floor after every tick, independently of the safety critic. No model calls.

The critic checks a *plan* before it runs; this monitor checks what actually *happens*, so a bug in a planner, a
roll-back, a pre-emption or a connector cannot put someone at risk unnoticed:

  I1  no moving AGV inside a fenced incident area while a person is in it   -> the AGV is stopped
  I2  an AGV on a pedestrian crossing moves at 0.5 m/s or less              -> the AGV is stopped
  I3  nobody inside a robot-cell / machine-repair fence without a valid lockout/tagout authorisation -> escalated
  I4  no two agents on one cell (collision)                                 -> recorded

Violations are appended to logs/invariants.jsonl and counted in Health.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

LOG = Path(__file__).resolve().parents[2] / "logs" / "invariants.jsonl"
LOTO_KINDS = {"robot_cell_fault", "overheat"}
CROSSING_LIMIT = 0.5


class InvariantMonitor:
    def __init__(self, plant=None, enforce: bool = True, log_path: Path | None = LOG, on_violation=None):
        self.plant, self.enforce, self.log_path, self.on_violation = plant, enforce, log_path, on_violation
        self.violations: list[dict] = []
        self._seen: dict[tuple, int] = {}

    def _report(self, sim, rule: str, agent: str, detail: str, fence: str | None = None, action: str = "recorded"):
        key = (rule, agent, fence)
        if sim.tick - self._seen.get(key, -99) < 10:  # once per 10 ticks for the same situation
            return
        self._seen[key] = sim.tick
        v = {"ts": datetime.now().astimezone().isoformat(), "tick": sim.tick, "rule": rule, "agent": agent,
             "fence": fence, "detail": detail, "action": action}
        self.violations.append(v)
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(v) + "\n")
        if self.on_violation:
            self.on_violation(v)

    def check(self, sim) -> list[dict]:
        before = len(self.violations)
        agents = list(sim.agents.values())
        for key, cells in list(sim.fences.items()):
            people = [a for a in agents if a.kind == "worker" and a.pos in cells]
            if people:
                for a in agents:
                    if a.kind == "agv" and a.pos in cells and a.status == "moving":
                        if self.enforce:
                            sim.stop(a.id)
                        self._report(sim, "I1", a.id, f"moving inside fence {key} with {len(people)} person(s)",
                                     key, "AGV stopped" if self.enforce else "recorded")
            kind = (getattr(sim, "fence_kinds", {}) or {}).get(key)
            if kind in LOTO_KINDS and self.plant is not None:
                for p in people:
                    w = self.plant.entity(p.id)
                    if w and not self.plant.cert_valid(w, "lockout_tagout") and p.status != "moving":
                        self._report(sim, "I3", p.id, f"inside {kind} area {key} without valid lockout/tagout", key,
                                     "escalated: a supervisor must remove the person")
        for a in agents:
            if a.kind == "agv" and a.status == "moving" and sim.grid.is_crossing(*a.pos):
                speed = getattr(a, "crossing_speed_mps", None)
                if speed is None or speed > CROSSING_LIMIT:
                    if self.enforce:
                        sim.stop(a.id)
                    self._report(sim, "I2", a.id, f"on a crossing at {speed or a.speed_mps} m/s",
                                 None, "AGV stopped" if self.enforce else "recorded")
        seen: dict = {}
        for a in agents:
            if a.pos in seen:
                self._report(sim, "I4", a.id, f"shares cell {a.pos} with {seen[a.pos]}")
            seen[a.pos] = a.id
        return self.violations[before:]

    def summary(self) -> dict:
        by: dict[str, int] = {}
        for v in self.violations:
            by[v["rule"]] = by.get(v["rule"], 0) + 1
        return {"violations": len(self.violations), "by_rule": by, "last": self.violations[-3:]}
