# Slide deck outline (covers all 8 required points)

The screenshots are in `docs/screenshots/`. Keep one idea per slide.

## 1. Title
**Shop-Floor OS: AI agents plan, robots execute, humans judge.** Open Agent Hackathon 2026, Track 3: Reasoning
Architecture. Team name and members.
Visual: `01-part-shortage-critic.png`, cropped to the map.

## 2. The problem and target users (point 1)
- On a car assembly line every stopped minute costs cars: one car every 75 s in our hall.
- Supervisors juggle kit shortages, AGV faults, spills and machine alarms. Each one involves safety rules,
  certifications and robot traffic.
- Many alarms are sensor noise. Stopping the line for a false alarm is as costly as ignoring a real one.
- **Users:** shift supervisors, team leaders and maintenance planners in vehicle assembly.

## 3. The solution (point 2)
- A multi-agent system that turns fragmented plant data into a safe, explained, verified dispatch decision in
  seconds.
- The decision can be ACT, VERIFY, SAFE_HOLD, ESCALATE or DO_NOTHING, and a human approves or rejects it.
- Every rejection and plan cites the exact OSHA clause or plant SOP.

Visual: `03-overheat-verify.png` (VERIFY instead of a line stop).

## 4. Agent workflow and architecture (point 3)
Diagram:
`reflex → monitor → memory (H-MEM) → candidates (Zetaris → World Model) → planner (Nemotron) ⇄ safety critic → dry-run → restraint → executor → verifier → human`

Callouts:
- one federated query per incident;
- strict Pydantic JSON between agents;
- bounded loops (3 critic tries, 3 re-plans);
- circuit breakers and safe mode;
- a JSONL trace for every step.

Visual: `02-agv-fault-replan.png` (the verifier re-plans after a stall).

## 5. Technical stack (point 4)
- Python 3.11+, mostly standard library. Pydantic contracts. A stdlib HTTP server with SSE. A vanilla JS canvas UI.
- Simulation: a 20×40 grid, space-time A* with a reservation table, and a seeded incident injector.
- Data: Postgres ×4 behind Zetaris. The synthetic plant is anchored on AI4I 2020, OSHA SIR, BLS SOII, Open-Meteo,
  the Bosch CNC dataset and MovingAI.
- Docker (tested). 83 offline tests. `SAMPLE_MODE` runs without keys.

## 6. How Zetaris and Meterless were used (point 5)
- **Zetaris:**
  - the data layer;
  - one Lightning SQL `UNION ALL` across `sf_plant`, `sf_hr`, `sf_fleet` and `sf_ops` per incident;
  - zone briefings and lab history;
  - lab proposals written back with `INSERT INTO sf_ops.experiments`.
- **Meterless World Model:** grounds every plan (stable ids, provenance, facts such as `service_overdue`). The
  planner may only use ids in the world. 8 conformance checks pass.
- **Meterless H-MEM:** incident lessons with outcome feedback, plus the OSHA and SOP clause index. 13 of 13
  official vectors pass.
- **Meterless Nightshift:** the only model provider, with deep and fast Nemotron lanes and circuit breakers.

Visual: `05-agents-know.png`.

## 7. What was unique about our use of the sponsor technology (point 6)
- **Three Meterless engines work together.** The World Model answers "what exists and is true"; H-MEM answers
  "what have we learned, and what does the regulation say"; Markovian turns a whole shift's journal into a
  handover note in bounded chunks, with the token savings measured.
- **The LLM is never trusted alone.** Nemotron proposes, a deterministic critic cites the CFR clause, a simulator
  dry-runs the plan, a restraint gate weighs cost, and a verifier checks reality.
- **Zetaris federation is the agents' only view of the plant.** One query, four sources, every incident.
- **Lab:** Nemotron's hypothesis is tested by LLM-free replays with a bootstrap CI, then written back through
  Zetaris.

## 8. Product visuals (point 7)
A collage of the screenshots:
- `01-part-shortage-critic.png`: critic rejection with the 29 CFR 1910.178(n)(4) citation;
- `02-agv-fault-replan.png`: stall detection and re-plan;
- `03-overheat-verify.png`: false alarm leads to VERIFY;
- `04-lab.png`: the policy experiment with its CI;
- `05-agents-know.png`: the agents' knowledge.

## 9. Results
- 3 live demo cases:
  - case 1: one critic rejection, then a cited retry;
  - case 2: one verifier re-plan;
  - case 3: VERIFY on a false alarm.
  - All of them had 0 collisions.
- Lab example: an extra spill kit at F20 cuts mean clean-up time by 8.2% (95% CI −11.1% to −5.3%), from 82
  re-scored incidents.
- 83 tests pass, including both engines' conformance suites.

## 10. Future enhancements (point 8)
- Arbitration between several incidents competing for the same AGVs and people.
- Live AGV and PLC feeds instead of the simulator (OPC UA / VDA 5050). Shift handover briefings from H-MEM.
- The Markovian engine for long multi-step maintenance plans. Scout Intent for free-text incident reports.
- Authentication and role-based approvals in the UI. A full CFR text index.

## 11. Thank you
Repository link, the demo command (`SAMPLE_MODE=true python -m app.server`), and the team.
