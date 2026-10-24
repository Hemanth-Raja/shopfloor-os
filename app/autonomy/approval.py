"""What happens when a person does not answer: the approval-timeout policy. Pure rules, no model calls.

A decision waits for a person for one of two kinds of reason (app/orchestrator.py gate_reasons):
- soft: only because the autonomy setting is Supervised. The plan already passed the critic and the dry run, and the
  restraint gate found that acting is cheaper than waiting.
- hard: a person would enter a hazard, a certificate has lapsed, a near miss needs a supervisor's sign-off, safe mode
  (Nightshift down), or the agents themselves chose ESCALATE, SAFE_HOLD or DO_NOTHING.

The clock runs in floor minutes from the moment the decision started waiting:
- REMIND_MIN: remind the approver.
- ESCALATE_MIN: escalate to the backup approver (the shift manager).
- DECIDE_MIN: the agents decide, but never in the unsafe direction:
  - soft + ACT or VERIFY: dispatch the plan ("auto_after_timeout"), audited as an automatic decision. Waiting longer only
    costs cars and the plan has every safety check behind it.
  - hard: never dispatch. Put the incident in a timed-out safe hold ("timeout_hold"): the fence and stopped AGVs
    stay, the people and robots it reserved are released for other incidents, and it stays escalated until a person
    answers. A late Approve still works.
- Advisory mode: the agents only recommend, so the clock only reminds and escalates.
"""

from __future__ import annotations

REMIND_MIN, ESCALATE_MIN, DECIDE_MIN = 2.0, 4.0, 6.0
SOFT_PREFIX = "supervised mode"


def is_soft(reasons: list[str]) -> bool:
    return bool(reasons) and all(r.startswith(SOFT_PREFIX) for r in reasons)


def next_action(trace, waited_min: float, autonomy: str) -> str | None:
    """-> None | "remind" | "escalate" | "auto_execute" | "hold" for a trace that is waiting for a person.
    Each action is returned once; the caller records it in trace.escalations."""
    if trace.gate not in ("awaiting_approval", "advisory"):
        return None
    done = set(trace.escalations)
    if waited_min >= DECIDE_MIN and autonomy != "advisory" and trace.gate == "awaiting_approval":
        if is_soft(trace.gate_reasons) and trace.decision is not None and trace.decision.decision in ("ACT", "VERIFY") \
                and "auto_execute" not in done:
            return "auto_execute"
        if not is_soft(trace.gate_reasons) and "hold" not in done:
            return "hold"
    if waited_min >= ESCALATE_MIN and "escalate" not in done:
        return "escalate"
    if waited_min >= REMIND_MIN and "remind" not in done:
        return "remind"
    return None
