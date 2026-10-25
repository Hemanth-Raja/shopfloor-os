"""Lab in the loop: apply a tested change to the floor, then measure the incidents that follow against the prediction.

The Lab predicts, from history, how many seconds a change saves per incident (`predicted_saving`). Apply puts the
change onto the floor (a spill kit or a waiting AGV at a station's line side; the map shows it). From then on every
dispatched incident of that kind is measured on the floor grid with the same A* walk the replays use: the walk
with the change and the walk the floor would have had without it. The summary sets the measured saving next to the
prediction. Deterministic, no model.
"""

from __future__ import annotations

import statistics
import threading
from datetime import datetime

from app.lab.experiment import Policy, staged_cell, travel

LIVE_KIND = {"move_spill_kit": "spill", "preposition_agv": "part_shortage"}  # history says fluid_spill


class LabLoop:
    def __init__(self):
        self.applied: dict | None = None
        self.measured: list[dict] = []
        self.lock = threading.Lock()
        self._cache: dict = {}

    def apply(self, result: dict, now: datetime | None = None) -> dict:
        if not result or not result.get("significant"):
            raise ValueError("run an experiment with a significant result first")
        h = result["hypothesis"]
        with self.lock:
            self.applied = {"lab_id": result.get("lab_id"), "policy": h["policy"], "hypothesis": h["hypothesis"],
                            "applied_at": (now or datetime.now().astimezone()).isoformat(),
                            "cell": list(staged_cell(h["policy"]["station"])), "kind": LIVE_KIND[h["policy"]["type"]],
                            "predicted": result.get("predicted_saving") or {}, "effect_pct": result.get("effect_pct"),
                            "ci": [result.get("ci_low_pct"), result.get("ci_high_pct")]}
            self.measured = []
        return self.summary()

    def undo(self) -> dict:
        with self.lock:
            self.applied, self.measured = None, []
        return self.summary()

    def measure(self, trace, grid) -> dict | None:
        """One finished incident: measured if the change applies to its kind and people or AGVs were dispatched."""
        a = self.applied
        inc = trace.incident
        sent = any(st.step == "executor" and st.ok for st in trace.steps) or getattr(trace, "gate", None) in (
            "approved", "auto_after_timeout")
        if a is None or inc.kind != a["kind"] or not sent:  # a plan still waiting for you has moved nobody yet
            return None
        policy = Policy(**a["policy"])
        old, new, per = travel(policy, (int(inc.x), int(inc.y)), inc.station_id, True, grid, self._cache)
        row = {"incident": inc.id, "station": inc.station_id or inc.zone_id, "cells_without": old, "cells_with": new,
               "saving_s": per * (old - new), "at": datetime.now().astimezone().isoformat()}
        with self.lock:
            if self.applied is a and all(m["incident"] != inc.id for m in self.measured):
                self.measured.append(row)
        return row

    def summary(self) -> dict:
        with self.lock:
            a, rows = self.applied, list(self.measured)
        if a is None:
            return {"applied": None}
        p = a["predicted"]
        at = [r["saving_s"] for r in rows if r["station"] == a["policy"]["station"]]
        return {"applied": a, "measured": rows[-50:], "n": len(rows),
                "measured_saving_s": round(statistics.fmean(r["saving_s"] for r in rows), 1) if rows else None,
                "measured_at_station_s": round(statistics.fmean(at), 1) if at else None, "at_station_n": len(at),
                "predicted_saving_s": p.get("per_incident_s"), "predicted_at_station_s": p.get("at_station_s")}
