"""Approval timeout policy: remind, escalate, then decide, never in the unsafe direction."""

from app.autonomy.approval import DECIDE_MIN, ESCALATE_MIN, REMIND_MIN, is_soft, next_action
from app.contracts import Incident, Plan, RestraintDecision, Trace


def trace(reasons, decision="ACT", gate="awaiting_approval"):
    inc = Incident(id="I-1", kind="part_shortage", station_id="F30", zone_id="FINAL", x=1, y=1, tick=0,
                   severity="high")
    return Trace(incident=inc, plan=Plan(incident_id="I-1", assignee_ids=["worker:plant:W-18"], priority="high",
                                         rationale="r"),
                 decision=RestraintDecision(decision=decision, score=0.9, reasons=[]), gate=gate, gate_reasons=reasons)


def walk(t, autonomy, minutes):
    out = []
    for m in minutes:
        a = next_action(t, m, autonomy)
        if a:
            t.escalations.append(a)
            out.append(a)
    return out


SUPERVISED = ["supervised mode: every dispatch waits for approval"]
HAZARD = ["a person would enter the spill hazard area"]


def test_soft_wait_is_reminded_escalated_then_dispatched():
    assert is_soft(SUPERVISED) and not is_soft(SUPERVISED + HAZARD) and not is_soft([])
    t = trace(SUPERVISED)
    assert walk(t, "supervised", [1, REMIND_MIN, ESCALATE_MIN, DECIDE_MIN, DECIDE_MIN + 5]) == \
        ["remind", "escalate", "auto_execute"]


def test_hazard_never_auto_dispatches_it_holds_safe():
    for reasons in (HAZARD, SUPERVISED + HAZARD, ["ESCALATE: the agents are not confident enough to act alone"],
                    ["W-22 has a lapsed certificate (lockout_tagout)"]):
        t = trace(reasons)
        acts = walk(t, "auto", [REMIND_MIN, ESCALATE_MIN, DECIDE_MIN, DECIDE_MIN * 10])
        assert "auto_execute" not in acts and acts[-1] == "hold"


def test_advisory_only_reminds_and_escalates_and_answered_traces_are_left_alone():
    t = trace(["advisory mode: the agents only recommend; a person dispatches"], gate="advisory")
    assert walk(t, "advisory", [REMIND_MIN, ESCALATE_MIN, DECIDE_MIN * 10]) == ["remind", "escalate"]
    for gate in ("approved", "rejected", "auto_executed", "timeout_hold"):
        assert next_action(trace(SUPERVISED, gate=gate), 99, "supervised") is None


def test_server_dispatches_a_soft_wait_and_holds_a_hazard_after_the_timeout(monkeypatch):
    monkeypatch.setenv("SAMPLE_MODE", "true")
    from app import server
    from app.autonomy.sentinel import TICKS_PER_MIN
    from tests.test_autonomy import run
    app = server.App(offline=True, replay=True)
    app.bus.publish = lambda *a: None
    soft = run(app.plant, app.sim, "part_shortage", "F30")
    soft.gate, soft.gate_reasons, soft.autonomy = "awaiting_approval", SUPERVISED, "supervised"
    hard = run(app.plant, app.sim, "spill", "F20")
    assert hard.gate == "awaiting_approval" and not is_soft(hard.gate_reasons)
    app.traces = {soft.incident.id: soft, hard.incident.id: hard}
    app._approval_timeouts()  # starts the clocks
    app.sim.tick += int(DECIDE_MIN * TICKS_PER_MIN) + 1
    for _ in range(3):
        app._approval_timeouts()
    assert soft.gate == "auto_after_timeout" and any(app.sim.agents[a].status == "moving" for a in soft.dispatch)
    assert hard.gate == "timeout_hold" and "hold" in hard.escalations
    rec = app.approve({"incident_id": hard.incident.id, "approve": True})  # a late answer still counts
    assert rec["gate"] == "approved"
