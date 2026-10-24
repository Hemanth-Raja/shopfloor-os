"""Resource leases: two incidents can never send the same worker or AGV. No model calls.

An incident takes a lease on every worker and AGV it dispatches. A lease names the incident, its risk and an expiry,
so a crashed or forgotten incident cannot hold a resource for ever (no deadlocks). A higher-risk incident may
pre-empt a lower-risk holder: the resource moves to it, and the loser is told to re-plan without that resource.
Equal or higher risk is never pre-empted.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

DEFAULT_TTL_S = 600.0


@dataclass
class Lease:
    resource: str
    incident: str
    risk: float
    expires: float


@dataclass
class LeaseBook:
    ttl_s: float = DEFAULT_TTL_S
    clock: Callable[[], float] = time.monotonic
    leases: dict[str, Lease] = field(default_factory=dict)
    preempted: dict[str, set[str]] = field(default_factory=dict)  # incident -> resources it lost
    log: list[dict] = field(default_factory=list)
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def _live(self, resource: str) -> Lease | None:
        lease = self.leases.get(resource)
        if lease and lease.expires <= self.clock():
            self.leases.pop(resource, None)
            self.log.append({"op": "expired", "resource": resource, "incident": lease.incident})
            return None
        return lease

    def holder(self, resource: str) -> Lease | None:
        with self._lock:
            return self._live(resource)

    def blocked_for(self, incident: str, risk: float) -> set[str]:
        """Resources this incident may not plan with: held by another incident of equal or higher risk."""
        with self._lock:
            return {r for r in list(self.leases) if (l := self._live(r)) and l.incident != incident and l.risk >= risk}

    def conflicts(self, incident: str, risk: float, resources) -> list[Lease]:
        with self._lock:
            return [l for r in resources if (l := self._live(r)) and l.incident != incident and l.risk >= risk]

    def acquire(self, incident: str, risk: float, resources) -> dict:
        """Take every resource; pre-empt lower-risk holders. Returns {granted, preempted: {resource: loser}, refused}."""
        out = {"granted": [], "preempted": {}, "refused": []}
        with self._lock:
            for r in resources:
                held = self._live(r)
                if held and held.incident != incident:
                    if held.risk >= risk:
                        out["refused"].append(r)
                        continue
                    out["preempted"][r] = held.incident
                    self.preempted.setdefault(held.incident, set()).add(r)
                    self.log.append({"op": "preempt", "resource": r, "from": held.incident, "to": incident})
                self.leases[r] = Lease(r, incident, risk, self.clock() + self.ttl_s)
                out["granted"].append(r)
                self.log.append({"op": "lease", "resource": r, "incident": incident})
        return out

    def take_preempted(self, incident: str) -> set[str]:
        with self._lock:
            return self.preempted.pop(incident, set())

    def release_all(self, incident: str) -> list[str]:
        with self._lock:
            gone = [r for r, l in self.leases.items() if l.incident == incident]
            for r in gone:
                del self.leases[r]
            if gone:
                self.log.append({"op": "release", "incident": incident, "resources": gone})
            self.preempted.pop(incident, None)
            return gone

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [{"resource": r, "incident": l.incident, "risk": l.risk,
                     "expires_in_s": round(l.expires - self.clock(), 1)}
                    for r in sorted(self.leases) if (l := self._live(r))]
