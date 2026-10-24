"""Sentinel: watches the floor's signal stream and the agents' own outcomes, and raises incidents.

    signals (stock, AGV status, machine temperature, camera, robot controller, torque, proximity, quality)
      -> rules: threshold and sensor checks (no model)
      -> de-duplicate (same kind and place within DEDUPE_TICKS) and correlate (an AGV fault absorbs the shortage
         it causes at the station it was feeding)
      -> ambiguous signals only: triage by the `sentinel` role (Nemotron 3.5 Lightning); rule-based without it
      -> incident -> dispatcher queue

Guardrails: a signal that may put a person at risk (spill, near miss) is never dismissed, whatever the triage
says; triage can only add a note or mark a likely false alarm, which the monitor and restraint gate then verify.
Every decision is appended to logs/sentinel.jsonl.

`generate_signals` produces a shift's signal stream from the historical incident mix in sf_ops.incidents
(deterministic for a seed), including duplicate alarms and correlated AGV-fault/shortage pairs.
"""

from __future__ import annotations

import json
import random
from datetime import datetime
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, ConfigDict, Field

LOG = Path(__file__).resolve().parents[2] / "logs" / "sentinel.jsonl"
TICKS_PER_MIN = 30  # one tick is 2 s
DEDUPE_TICKS = 90  # 3 minutes
CORRELATE_TICKS = 30
PEOPLE_KINDS = {"spill", "near_miss"}
STAT_TO_KIND = {"machine_overheat": "overheat", "fluid_spill": "spill"}
# historical mix (sf_ops.incidents counts) when the plant stats carry none
DEFAULT_MIX = {"part_shortage": 821, "robot_cell_fault": 274, "quality_defect": 268, "torque_fault": 225,
               "agv_fault": 213, "overheat": 100, "near_miss": 88, "spill": 82}
WHERE = {"part_shortage": ["F10", "F20", "F30", "T10", "T20", "M10"], "overheat": ["F10", "B10", "P10", "M10"],
         "spill": ["F20", "M10", "T20"], "robot_cell_fault": ["B10", "B20", "P10"], "torque_fault": ["F10", "F30", "M10"],
         "near_miss": ["F30", "F20", "T10"], "quality_defect": ["E10", "F30", "T20", "P10"],
         "agv_fault": ["F30", "F10", "F20", "T20"]}


class Triage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    raise_incident: bool
    likely_false_alarm: bool
    reason: str = Field(min_length=3, max_length=300)


def generate_signals(plant, minutes: float, seed: int, rate_per_min: float = 1.0) -> list[dict]:
    rng = random.Random(seed)
    counts = {STAT_TO_KIND.get(k, k): (v or {}).get("count") or 0 for k, v in (plant.stats or {}).items()}
    mix = counts if len(set(counts.values())) > 1 and sum(counts.values()) > 0 else DEFAULT_MIX
    mix = {k: v for k, v in mix.items() if k in WHERE}
    total = TICKS_PER_MIN * minutes
    n = max(1, round(minutes * rate_per_min))
    kitting = sorted(e["id"].split(":")[-1] for e in plant.entities("agv") if e["attrs"].get("kind") == "kitting")
    used_agvs: set[str] = set()
    out = []
    for i in range(n):
        tick = int(5 + (total - 10) * (i + rng.random() * 0.8) / n)
        kind = rng.choices(list(mix), weights=list(mix.values()))[0]
        station = rng.choice(WHERE[kind])
        sig = {"tick": tick, "station": station, "truth_kind": kind}
        if kind == "part_shortage":
            sig.update(type="line_stock", minutes_left=round(rng.uniform(0.8, 4.5), 1))
        elif kind == "agv_fault":
            free = [a for a in kitting if a not in used_agvs] or kitting
            agv = rng.choice(free)
            used_agvs.add(agv)
            sig.update(type="agv_status", agv=agv, fault_code=rng.choice(["E12_WHEEL_SLIP", "E40_BATTERY_LOW",
                                                                         "E52_NAV_LOST"]), feeding=station)
            if rng.random() < 0.6:  # the shortage it causes downstream shows up a little later
                out.append({"tick": tick + rng.randint(3, 10), "station": station, "truth_kind": "part_shortage",
                            "type": "line_stock", "minutes_left": round(rng.uniform(1.0, 3.0), 1), "caused_by": agv})
        elif kind == "overheat":
            false = rng.random() < 0.3
            sig.update(type="machine_temp", process_temp_k=round(rng.uniform(313.5, 316.0), 1), threshold_k=313.0,
                       vibration="normal" if false else "abnormal", truth_false_alarm=false)
        elif kind == "spill":
            sig.update(type="camera", what="fluid on floor", confidence=round(rng.uniform(0.55, 0.97), 2))
        elif kind == "robot_cell_fault":
            sig.update(type="robot_controller", code=rng.choice(["SRVO-050", "SRVO-062", "light curtain trip"]))
        elif kind == "torque_fault":
            false = rng.random() < 0.3
            sig.update(type="torque", nok_in_row=1 if false else rng.randint(2, 5), truth_false_alarm=false)
        elif kind == "near_miss":
            sig.update(type="proximity", min_distance_m=round(rng.uniform(0.3, 0.95), 2))
        else:
            sig.update(type="quality", defect=rng.choice(["seal_gap", "missing_clip", "paint_run", "torque_out_of_spec"]),
                       units=rng.randint(1, 4))
        out.append(sig)
        if rng.random() < 0.25:  # the same alarm fires again
            out.append({**sig, "tick": tick + rng.randint(4, 20), "repeat": True})
    return sorted(out, key=lambda s: s["tick"])


class Sentinel:
    def __init__(self, triage_llm: Callable | None = None, log_path: Path | None = LOG, on_event: Callable | None = None):
        self.triage_llm = triage_llm
        self.log_path = log_path
        self.on_event = on_event or (lambda kind, data: None)
        self.open: dict[tuple, dict] = {}  # (kind, place) -> {"incident", "tick", "merged"}
        self.per_station: dict[str, int] = {}
        self.stats = {"signals": 0, "raised": 0, "deduped": 0, "correlated": 0, "ignored": 0, "triaged": 0,
                      "dismissed": 0}

    def close(self, incident_id: str) -> None:
        """The incident is handled: a new alarm at the same place is a new incident again."""
        for key, o in list(self.open.items()):
            if o["incident"] == incident_id:
                self.open.pop(key)

    # ---------------------------------------------------------------- rules

    @staticmethod
    def classify(sig: dict) -> dict | None:
        """-> {kind, severity, ambiguous, false_hint, note} or None when the reading is normal."""
        t = sig["type"]
        if t == "line_stock":
            if sig["minutes_left"] >= 5:
                return None
            return {"kind": "part_shortage", "severity": "critical" if sig["minutes_left"] < 1.5 else "high",
                    "note": f"line-side stock {sig['minutes_left']} min"}
        if t == "agv_status":
            return {"kind": "agv_fault", "severity": "medium", "note": f"{sig['agv']} {sig['fault_code']}"}
        if t == "machine_temp":
            if sig["process_temp_k"] <= sig["threshold_k"]:
                return None
            ambiguous = sig["vibration"] == "normal"
            return {"kind": "overheat", "severity": "high", "ambiguous": ambiguous, "false_hint": ambiguous,
                    "note": f"{sig['process_temp_k']} K over {sig['threshold_k']} K, vibration {sig['vibration']}"}
        if t == "camera":
            return {"kind": "spill", "severity": "high", "ambiguous": sig["confidence"] < 0.7,
                    "note": f"camera {sig['what']} ({sig['confidence']:.0%})"}
        if t == "robot_controller":
            return {"kind": "robot_cell_fault", "severity": "high", "note": f"controller {sig['code']}"}
        if t == "torque":
            single = sig["nok_in_row"] < 2
            return {"kind": "torque_fault", "severity": "medium" if single else "high", "ambiguous": single,
                    "false_hint": single, "note": f"{sig['nok_in_row']} NOK in a row"}
        if t == "proximity":
            if sig["min_distance_m"] >= 1.0:
                return None
            return {"kind": "near_miss", "severity": "critical" if sig["min_distance_m"] < 0.5 else "high",
                    "note": f"AGV passed {sig['min_distance_m']} m from a person"}
        if t == "quality":
            return {"kind": "quality_defect", "severity": "medium" if sig["units"] > 2 else "low",
                    "note": f"{sig['defect']} on {sig['units']} unit(s)"}
        return None

    def _log(self, tick: int, sig: dict, action: str, **extra) -> None:
        rec = {"ts": datetime.now().astimezone().isoformat(), "tick": tick, "action": action,
               "signal": {k: v for k, v in sig.items() if not k.startswith("truth")}, **extra}
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, default=str) + "\n")
        self.on_event("sentinel", rec)

    def _triage(self, sig: dict, c: dict) -> tuple[bool, bool, str, str]:
        """-> (raise, likely_false_alarm, reason, by). People-risk kinds are never dismissed."""
        self.stats["triaged"] += 1
        if self.triage_llm is not None:
            try:
                reply = self.triage_llm("fast", [
                    {"role": "system", "content": "You triage shop-floor sensor signals in a car assembly hall. Decide "
                     "whether this ambiguous signal should become an incident, and whether it is likely a false "
                     "alarm. A person-safety signal must always be raised."},
                    {"role": "user", "content": json.dumps({"signal": {k: v for k, v in sig.items()
                                                                       if not k.startswith("truth")},
                                                            "rule_reading": c})}], Triage, timeout_s=30, role="sentinel")
                t = reply.data if hasattr(reply, "data") else reply
                by = f"nemotron ({getattr(reply, 'model', '?')})"
                raise_it = t.raise_incident or c["kind"] in PEOPLE_KINDS
                return raise_it, t.likely_false_alarm, t.reason, by
            except Exception as err:  # noqa: BLE001 - fall back to the rules
                return True, bool(c.get("false_hint")), f"triage unavailable ({type(err).__name__}): raised to verify", "rules"
        return True, bool(c.get("false_hint")), "ambiguous reading: raised so the monitor can verify it", "rules"

    # ---------------------------------------------------------------- the watch

    def process(self, sig: dict, tick: int, raise_incident: Callable[[dict], object]) -> object | None:
        """Handle one signal. `raise_incident(spec)` creates the incident and returns it (or None)."""
        self.stats["signals"] += 1
        c = self.classify(sig)
        if c is None:
            self.stats["ignored"] += 1
            self._log(tick, sig, "normal")
            return None
        place = sig.get("agv") if c["kind"] == "agv_fault" else sig.get("station")
        key = (c["kind"], place)
        for (k, p), o in list(self.open.items()):  # forget what is older than the window
            if tick - o["tick"] > DEDUPE_TICKS:
                self.open.pop((k, p))
        if key in self.open:
            self.open[key]["merged"] += 1
            self.stats["deduped"] += 1
            self._log(tick, sig, "deduplicated", incident=self.open[key]["incident"])
            return None
        if c["kind"] == "part_shortage":  # caused by an AGV fault we already handle?
            cause = next((o for (k, _), o in self.open.items() if k == "agv_fault" and o.get("feeding") == sig["station"]
                          and tick - o["tick"] <= CORRELATE_TICKS), None)
            if cause:
                cause["merged"] += 1
                self.stats["correlated"] += 1
                self._log(tick, sig, "correlated", incident=cause["incident"],
                          note=f"shortage at {sig['station']} is caused by {cause['incident']}'s AGV fault")
                return None
        false_alarm, why, by = bool(c.get("false_hint")), None, "rules"
        if c.get("ambiguous"):
            raise_it, false_alarm, why, by = self._triage(sig, c)
            if not raise_it:
                self.stats["dismissed"] += 1
                self._log(tick, sig, "dismissed", reason=why, by=by)
                return None
        station = sig.get("station")
        n = self.per_station.get(station, 0)
        severity, note = c["severity"], c["note"]
        if n >= 2 and severity != "critical":  # the same station keeps failing this shift
            severity = {"low": "medium", "medium": "high", "high": "critical"}[severity]
            note += f"; incident {n + 1} at {station} this shift"
        spec = {"kind": c["kind"], "station": station, "agv": sig.get("agv"), "severity": severity, "note": note[:300],
                "false_alarm": bool(sig.get("truth_false_alarm")) if c["kind"] in ("overheat", "torque_fault") else None}
        inc = raise_incident(spec)
        if inc is None:
            self._log(tick, sig, "not raised", reason="the floor could not place it")
            return None
        self.per_station[station] = n + 1
        self.open[key] = {"incident": inc.id, "tick": tick, "merged": 0, "feeding": sig.get("feeding")}
        self.stats["raised"] += 1
        self._log(tick, sig, "raised", incident=inc.id, kind=c["kind"], severity=severity, triage=why, by=by,
                  likely_false_alarm=false_alarm)
        return inc
