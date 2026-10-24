# Demo video script (target 3:50, hard limit 4:00)

**Setup before recording:**
- Zetaris, `shopfloor-pg` and Nightshift are running. Check with `run.py health`: all ok.
- Start the UI with `.venv/bin/python -m app.server` and open http://127.0.0.1:8000. A terminal is open beside it.
- For a recording with no keys, use `SAMPLE_MODE=true` instead. It replays recorded Nemotron runs. If you do,
  say so on screen.
- Never show `.env`. Close any window with tokens in it.

| Time | On screen | Voice-over (about 150 words per minute) |
|---|---|---|
| 0:00–0:20 | Title card, then the Live floor tab | "On a car assembly line, a missing kit, a stalled robot or a coolant spill stops cars and can hurt people. Supervisors decide in seconds who to send, which robot to re-route, and whether an alarm is even real. Shop-Floor OS: AI agents plan, robots execute, humans judge." |
| 0:20–0:40 | Pan over the map: stations, AGVs 1–8, the X-WEST and X-EAST crossings, the supermarket. Then the header chips | "This is a fictional final-assembly hall. The data comes from four databases, fleet, people, plant and operations, all federated through Zetaris. The agents reason with Nemotron through Meterless Nightshift, on two Meterless engines: World Model and H-MEM." |
| 0:40–1:25 | **Case 1:** click *Part shortage · F30*. Let the trace stream; pause on the red critic line and the clause box | "A kit is short at F30. A reflex layer reacts in under a millisecond. Then one federated Zetaris query builds the World Model, and the candidates agent picks qualified people and healthy robots. Nemotron plans: send AGV-5 at 1 metre per second. The safety critic rejects it. The route crosses a pedestrian walkway, and here is the exact clause, 29 CFR 1910.178(n)(4). The planner retries at 0.5 metres per second and cites the same clause. A 60-second dry run checks it, the restraint gate says ACT, and the verifier confirms the world changed as predicted." |
| 1:25–2:05 | **Case 2:** click *AGV-3 fault + stall*. Highlight the red AGV and the geofence, the verifier mismatch line, and the second planner | "Now AGV-3, our oldest robot, faults on the lane. It's fenced off; the plan sends a technician with the reset skill and a replacement AGV. Mid-route the replacement stalls too. The verifier sees the world didn't move as predicted, excludes that AGV and re-plans with AGV-2. No collisions: every path comes from A* with a reservation table." |
| 2:05–2:35 | **Case 3:** click *Overheat · F10 (false alarm)*. Show the monitor line and VERIFY | "An overheat alarm at the wheel nutrunner. The temperature is over the threshold, but a real Bosch vibration recording says normal. The sensors disagree, so instead of stopping a line that loses a car every 75 seconds, the restraint gate says VERIFY: send a technician first. And a human still approves or rejects every decision." Click **Approve**. |
| 2:35–2:55 | **Same incident, two autonomy levels.** Switch the Decision panel from *Auto* to *Supervised*, then click *Part shortage · F30* again. Point at the amber "Waiting for you" box and the still robots. Press **Approve & dispatch** and the AGV moves. Switch back to *Auto* | "How much the agents do alone is a choice. In Auto, a routine, safe decision runs by itself, and you can still roll it back. Anything risky, like a person entering a spill or a lapsed certificate, always waits. In Supervised, nothing moves until I approve. Same incident, same plan: now a human holds the trigger." |
| 2:55–3:20 | **Lab** tab: click *Run experiment*. Show the hypothesis, the CI bar, the replays and *Save proposal to Zetaris* | "The lab turns history into policy. Nemotron proposes a change, for example staging a loaded AGV at F10. We replay the real incident history 20 times with seeds and no LLM, and bootstrap a 95% confidence interval. Only a significant effect becomes a proposal, written back through Zetaris." |
| 3:20–3:35 | **Ask the floor** tab: click *Why does F10 keep stopping?*. Show the steps, then the evidence column with the SQL and rows | "And you can ask the floor what happened. A coordinator splits the question; SQL agents query Zetaris through a read-only guard; a log agent reads our own traces; the analyst answers, and a critic drops any claim the rows don't support. Part shortages from AGV downtime, then torque faults on M-06." |
| 3:35–3:50 | Terminal: `run.py run input_examples/example_1.json`, `pytest -q` (115 passed), then `tail logs/trace-example-1.jsonl` | "Judges can run it as JSON in and JSON out, read a trace of every agent step and which model answered, and run 115 offline tests, including both Meterless engines' conformance suites. Shop-Floor OS: reliable decisions, not just answers." |

**Checks before export:**
- Under 4:00.
- No keys on screen.
- Each case is shown from a real run, live or replay.
- Zetaris and Meterless are each named at least twice.
