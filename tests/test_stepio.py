"""Every agent step gets an input rebuilt from the incident and the earlier outputs it reads."""

from app.contracts import Incident, StepResult, Trace
from app.stepio import step_inputs


def test_inputs_follow_the_outputs_of_earlier_steps():
    inc = Incident(id="SIM-1-001", kind="part_shortage", station_id="F30", zone_id="FINAL", x=1, y=1, tick=0, severity="high")
    t = Trace(incident=inc)
    for name, d in [("reflex", {}), ("monitor", {"verdict": "real", "p_real": 0.95, "sensor_agreement": True}),
                    ("memory", {"playbook": None, "lessons": []}),
                    ("candidates", {"workers": ["worker:plant:W-18"], "agvs": ["agv:plant:AGV-5"]}),
                    ("planner", {"version": 1, "attempt": 1, "assignees": ["worker:plant:W-18", "agv:plant:AGV-5"]}),
                    ("critic", {"plan_source": "rules v1", "violations": []}), ("dryrun", {"reasons": [], "eta_s": {}}),
                    ("critic", {"version": 2, "plan_source": "nemotron v2", "violations": [{"rule_id": "R-CROSSING"}]}),
                    ("refine", {"chosen": "v1"}), ("restraint", {"decision": "ACT"}), ("gate", {"autonomy": "auto"})]:
        t.steps.append(StepResult(step=name, ok=True, ms=0.0, detail=d))
    ins = step_inputs(t)
    assert len(ins) == len(t.steps)
    assert ins[0]["incident"] == "part_shortage at F30"
    assert ins[4]["people"] == ["W-18"] and ins[4]["agvs"] == ["AGV-5"]  # planner reads the candidates
    assert ins[5]["plan"] == {"assignees": ["W-18", "AGV-5"]}  # the critic reads the plan
    assert ins[8]["v2 critic violations"] == ["R-CROSSING"]
    assert ins[9]["critic ok"] is True and ins[9]["p_real"] == 0.95  # restraint sees the chosen plan's check
    assert ins[10]["decision"] == "ACT"
