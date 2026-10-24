"""Connectors to plant systems. The simulator implements each one; a real plant swaps in its own adapter.

    FleetManager  dispatch people and AGVs (fencing tokens, idempotency keys)
    MES           line status and quality holds
    PLC           read sensor signals

Only `SimFleet` sits on the dispatch path today. The guarantees it enforces are the ones a real adapter must keep:
- **Fencing tokens:** every lease grants a token that only ever increases. A command carrying a token lower than the
  last one seen for that agent comes from a holder that was pre-empted or timed out: it is refused.
- **Idempotency keys:** a retried dispatch with the same key returns the first result instead of moving the agent
  twice.
"""

from __future__ import annotations

import threading
from typing import Protocol


class StaleToken(PermissionError):
    """A dispatch from a lease holder that has since lost the resource."""


class FleetManager(Protocol):
    def dispatch(self, agent: str, goal, speed_mps: float, crossing_speed_mps, via, token: int | None,
                 key: str | None): ...


class MES(Protocol):
    def line_status(self) -> dict: ...

    def hold_units(self, station: str, units: int, reason: str) -> dict: ...


class PLC(Protocol):
    def read(self, tag: str): ...


class SimFleet:
    """FleetManager on the simulator, with fencing and idempotency."""

    def __init__(self, sim):
        self.sim = sim
        self.last_token: dict[str, int] = {}
        self.done: dict[str, object] = {}
        self.refused: list[dict] = []
        self.lock = threading.Lock()

    def dispatch(self, agent, goal, speed_mps=1.0, crossing_speed_mps=None, via=None, token=None, key=None):
        with self.lock:
            if key is not None and key in self.done:
                return self.done[key]  # the same command again: no second move
            if token is not None:
                if token < self.last_token.get(agent, 0):
                    self.refused.append({"agent": agent, "token": token, "current": self.last_token[agent]})
                    raise StaleToken(f"{agent}: token {token} < {self.last_token[agent]} (lease lost)")
                self.last_token[agent] = token
            path = self.sim.assign(agent, goal, speed_mps, crossing_speed_mps, via=via)
            if key is not None:
                self.done[key] = path
            return path


class SimMES:
    def __init__(self):
        self.holds: list[dict] = []

    def line_status(self) -> dict:
        return {"running": True, "holds": len(self.holds)}

    def hold_units(self, station: str, units: int, reason: str) -> dict:
        rec = {"station": station, "units": units, "reason": reason}
        self.holds.append(rec)
        return rec


class SimPLC:
    def __init__(self, signals: dict | None = None):
        self.signals = signals or {}

    def read(self, tag: str):
        return self.signals.get(tag)
