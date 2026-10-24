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
| 0:20–0:35 | Pan over the map, then the header chips | "A fictional final-assembly hall. Four databases, fleet, people, plant and operations, federated through Zetaris. The agents reason with Nemotron through Meterless Nightshift, on two Meterless engines: World Model and H-MEM." |
| 0:35–1:10 | **Case 1:** click *Part shortage · F30*. Pause on the red critic line and the clause | "A kit is short at F30. The reflex fences the area in under a millisecond. Then an orchestrator agent decides which agent runs next. Candidates come from the World Model; Nemotron plans AGV-5 at 1 metre per second. The safety critic rejects it: the route crosses a walkway, 29 CFR 1910.178(n)(4). The orchestrator sends it back to the planner, which slows to 0.5 at the crossing. Dry run, restraint, ACT, verified." |
| 1:10–1:40 | **Two incidents, two routes:** click *Part shortage · F30* again. Open the **Flow** tab, tick *compare with the previous incident* | "Same incident again. This time the orchestrator takes a different route: memory finds a proven H-MEM playbook, so it skips the planner entirely. No model call, no critic rejection. Each arrow is numbered, with the reason for the hop and who chose it. Code decides what is legal: the critic, the dry run and the human gate can never be skipped." |
| 1:40–2:05 | **Case 2:** *Overheat · F10 (false alarm)*. Show the route: monitor, investigator, check first | "An overheat alarm, but the Bosch vibration recording says normal. The sensors disagree, so the orchestrator looks up the history in Zetaris first, then sends one technician to check. No planner. VERIFY, and it waits for a human." Click **Approve**. |
| 2:05–2:25 | **Case 3:** *AGV-3 fault + stall*. Highlight the verifier mismatch and the route back to candidates | "AGV-3 faults; the replacement stalls mid-route. The verifier sees the world didn't move as predicted, and the orchestrator goes back to candidates without that robot, then re-plans with AGV-2." |
| 2:25–2:40 | Switch to *Supervised*, click *Part shortage · F30*, press **Approve & dispatch**, switch back to *Auto* | "How much the agents do alone is a choice. In Supervised, nothing moves until I approve." |
| 2:40–3:05 | **Ask the floor** tab: *Why does F10 keep stopping?* Show the evidence column | "You can also ask the floor what happened. A coordinator splits the question; SQL agents query Zetaris through a read-only guard; a log agent reads our traces; the analyst answers; two critics, rules and a second model, drop anything the rows don't support." |
| 3:05–3:20 | **Lab** tab: *Run experiment* | "The lab turns history into policy: Nemotron proposes a change, 20 seeded replays and a bootstrap confidence interval decide, and the proposal is written back through Zetaris." |
| 3:20–3:35 | **Flow** tab, *Models* panel | "Each agent role has its own model chain, chosen by capability: fast models route, the deep model plans, a separate model critiques the analyst. Each call records which model really answered, and its Meterless receipt." |
| 3:35–3:50 | Terminal: `run.py run input_examples/example_3.json`, `pytest -q` | "JSON in, JSON out, a trace of every hop, and an offline test suite including the Meterless conformance tests and the guardrails. Shop-Floor OS: reliable decisions, not just answers." |

**Checks before export:**
- Under 4:00.
- No keys on screen.
- Each case is shown from a real run, live or replay.
- Zetaris and Meterless are each named at least twice.
