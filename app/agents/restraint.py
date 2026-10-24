"""Restraint gate: should we act, verify first, hold safe, escalate or do nothing?

The inputs are confidence that the alarm is real, agreement between sensors, the dry-run result, and the
expected cost of waiting against the cost of acting. Both costs use the historical cost_usd per incident
kind from sf_ops.incidents, read through the plant world model.
"""

from __future__ import annotations

from app.contracts import Incident, RestraintDecision

STAT_KIND = {"part_shortage": "part_shortage", "agv_fault": "agv_fault", "overheat": "machine_overheat",
             "spill": "fluid_spill"}
HAZARD = {"spill", "overheat"}
LABOUR_USD_PER_HOUR = 85
LINE_STOP_USD_PER_MIN = 1500
ACT_STOP_MIN = {"overheat": 10, "spill": 3, "part_shortage": 0, "agv_fault": 0}  # line stop that acting itself costs


def decide(incident: Incident, monitor: dict, dryrun: dict | None, plan_ok: bool, stats: dict) -> RestraintDecision:
    p = monitor["p_real"]
    avg_cost = (stats.get(STAT_KIND[incident.kind]) or {}).get("avg_cost_usd") or 5000.0
    cost_wait = round(p * avg_cost, 0)
    cost_act = round(0.5 * LABOUR_USD_PER_HOUR + ACT_STOP_MIN[incident.kind] * LINE_STOP_USD_PER_MIN, 0)
    reasons = [f"p_real={p:.2f}", f"sensors agree={monitor['sensor_agreement']}",
               f"expected cost of waiting ${cost_wait:,.0f} vs acting ${cost_act:,.0f}"]
    score = round(p * (cost_wait / max(cost_wait + cost_act, 1)), 3)
    if not plan_ok:
        return RestraintDecision(decision="ESCALATE", score=score, reasons=reasons + ["no safe plan after retries"])
    if p < 0.1:
        return RestraintDecision(decision="DO_NOTHING", score=score, reasons=reasons + ["alarm almost certainly noise"])
    if p < 0.5 and not monitor["sensor_agreement"]:
        return RestraintDecision(decision="VERIFY", score=score,
                                 reasons=reasons + ["sensors disagree: send a technician to check before stopping the line"])
    if dryrun is not None and not dryrun["ok"]:
        if incident.kind in HAZARD:
            return RestraintDecision(decision="SAFE_HOLD", score=score,
                                     reasons=reasons + ["dry-run failed on a hazard: hold the area safe"] + dryrun["reasons"])
        return RestraintDecision(decision="ESCALATE", score=score, reasons=reasons + ["dry-run failed"] + dryrun["reasons"])
    if cost_wait >= cost_act or incident.kind in HAZARD:
        return RestraintDecision(decision="ACT", score=score, reasons=reasons + ["acting is cheaper than waiting"])
    return RestraintDecision(decision="VERIFY", score=score, reasons=reasons + ["waiting is cheaper: verify first"])
