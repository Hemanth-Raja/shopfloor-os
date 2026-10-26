# Output examples

One JSON result per input in [`input_examples/`](../input_examples), produced by
`SAMPLE_MODE=true .venv/bin/python run.py run input_examples/example_N.json`. A fresh run in sample mode on 26 Oct
2026 gave the same decision, gate and agent route for all eight. Nothing is read from this folder at run time.
`output.path` lists every hop, who chose it (`code`, `forced` or `rules`) and why.

**[Example 1](example_1.json): part shortage at F30.** *Input:* a missing kit at F30. *What the agents did:* the
planner's rules plan (v1) sends worker W-18 and AGV-5, slowing to 0.5 m/s at crossings. Nemotron's recorded plan (v2)
drives AGV-5 through the X-WEST crossing at 1.0 m/s, and the critic rejects it under 29 CFR 1910.178(n)(4), so v1 is
kept. *Decision:* ACT, auto-executed. *Next for the floor lead:* nothing is required. The kit run is under way, and
it can be rolled back from the *Decision* panel.

**[Example 2](example_2.json): AGV-3 fault with a stall.** *Input:* AGV-3 faults on the way to F30, and the
replacement is made to stall after 2 ticks. *What the agents did:* the first plan sends AGV-1, which stalls. The
verifier sees it is not where it should be and sends the work back to candidates without AGV-1; the second round
sends AGV-2 with W-18. *Decision:* ACT, 1 re-plan, auto-executed. *Next:* get AGV-1 and AGV-3 to maintenance; the
agents no longer use either.

**[Example 3](example_3.json): overheat at F10 that is a false alarm.** *Input:* a temperature alarm at F10 with
`false_alarm: true`. *What the agents did:* the monitor sees the temperature over its threshold but a normal Bosch
vibration recording, so the sensors disagree. The investigator reads the history, and *check first* sends one
maintenance technician (W-19), with no planner and no model call. *Decision:* VERIFY, waiting for approval because a person
would enter the overheat area. *Next:* approve the inspection; the line keeps running meanwhile.

**[Example 4](example_4.json): coolant spill at F20.** *Input:* a spill at F20 (seed 7). *What the agents did:* the
reflex puts up a geofence and keeps AGVs out; the planner sends team leader W-18 (spill response), citing 29 CFR 1910.22(a)(1)
and SOP-SPILL-01 §2. *Decision:* ACT, waiting for approval because a person enters the spill area. *Next:* approve
or reject; until then the area stays fenced.

**[Example 5](example_5.json): weld robot fault at B10.** *Input:* a high-severity robot-cell fault with the note
"weld robot stopped mid-cycle". *What the agents did:* the cell is fenced; the plan sends maintenance technician W-19, who
holds in-date lockout/tagout and robot-cell entry (SOP-ROBOT-01, SOP-ROBOT-02). *Decision:* ACT, waiting for
approval because a person enters the robot cell. *Next:* approve, once lockout/tagout is confirmed on site.

**[Example 6](example_6.json): nutrunner torque fault at F10.** *Input:* a torque fault at F10. *What the agents
did:* v1 sends a line operator with torque tools (W-16) and a quality inspector who audits torque (W-20). Nemotron's v2 sends only W-16, and
the critic rejects it because joints made since the last good torque must be re-audited (SOP-TORQUE-01 §2). v1 is
kept. *Decision:* ACT, auto-executed. *Next:* hold the affected units until the auditor's re-check is back.

**[Example 7](example_7.json): pedestrian near miss at F30.** *Input:* "AGV passed close to an operator at the
crossing". *What the agents did:* the AGV involved is held where it stopped, and team leader W-18 (incident command) secures the scene (SOP-NEARMISS-01,
29 CFR 1910.178(m)(1)). *Decision:* ACT, waiting for a supervisor's sign-off before the AGV is released. *Next:*
review the near miss and sign off.

**[Example 8](example_8.json): quality defect at end-of-line.** *Input:* a low-severity defect at E10. *What the
agents did:* a quality inspector (W-20) contains the affected units (SOP-QUAL-01 §1). *Decision:* ACT,
auto-executed. *Next:* nothing is required.
