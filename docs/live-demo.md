# Live demo runbook

## 1. Before you start (about 5 minutes)
1. **Zetaris** must be running. In WSL:
   ```
   docker ps --format '{{.Names}} {{.Status}}' | grep zetaris
   ```
   You should see `zetaris-api-1`, `zetaris-ui-1`, `zetaris-driver-1` and `zetaris-postgres-1` as healthy. If they
   aren't, start Zetaris from its install folder: `cd ~/hackathon/zetaris-platform && docker compose up -d`.
   Never use `down -v`.
2. **Meterless Nightshift** must be running on Windows, with the NVIDIA provider set up in its Providers tab.
3. **One command does the rest.** It recreates the Postgres container if it is missing, loads the data if it is
   empty, refreshes Zetaris, checks health and starts the UI on a free port:
   ```
   cd ~/hackathon/shopfloor-os
   bash scripts/start_live.sh
   ```
   You should see `zetaris: ok`, `nightshift: ok` and then `Starting the UI on http://127.0.0.1:8100` (or the next
   free port). Open that address.
4. Press **Ctrl+F5** in the browser. Check the header says `mode live` and the Health panel shows Zetaris and
   Nightshift as ok.

### Seeing what happens, live
- **Incidents tab** (next to Live floor): every incident with its agent flow as a graph. The active agent lights up, and the critic-retry and re-plan loops count up. Click any agent for its full input and output: what was sent to Nemotron, which model answered, and how long it took.
- **Second terminal:** `cd ~/hackathon/shopfloor-os && .venv/bin/python run.py watch`. This prints one line per step, Nemotron call and approval.
- **Raw logs:** each incident writes its own file, `logs/trace-<incident id>.jsonl`; `logs/llm.jsonl` records every Nemotron call. For example `tail -f logs/llm.jsonl`, or `ls -t logs/trace-*.jsonl | head -1 | xargs tail -f` for the newest incident. The old single `logs/trace.jsonl` is no longer written.

### Which model does what
The **Models** panel (*Nemotron at work* tab) lists each agent role, its model chain from `config/models.json`, breaker states
and the last call: which model answered, latency and the Nightshift receipt id. Run
`.venv/bin/python scripts/probe_models.py` beforehand to refresh which models Nightshift really serves. With a
single lane configured, every NVIDIA id is answered by Nemotron 3.5 Super VL, and the panel says so.

### Showing that Meterless Nightshift does the AI work
- The **Powered by** strip at the top of our UI shows the last Nightshift call (model, latency, time) and how many **signed compute receipts** Nightshift has written for app `shopfloor-os`, with the latest receipt's summary, e.g. `ok · Nemotron 3.5 Super VL 120B A12B`.
- Every request from `app/llm.py` carries the header `meterless-app: shopfloor-os`, so Nightshift attributes it to our app. The receipts are in Nightshift's state folder: on Windows `%LOCALAPPDATA%\ai.meterless.runtime\receipts.jsonl`, one signed JSON receipt per request with `"app": "shopfloor-os"`.
- Nightshift's **Night → Live field** view is for overnight batch jobs. Our app does not use it, so a lane showing "silent" there is unrelated. Look for our requests in Nightshift's daytime runtime and receipts views (for example MONITOR in the left rail), or in the receipts file above.

## 2. The demo flow (follow `docs/demo-script.md`)
| Step | Click | What the audience sees | Live timing |
|---|---|---|---|
| 1 | *Part shortage · F30* (Auto) | The critic rejects the plan (AGV too fast at the crossing) with the clause 29 CFR 1910.178(n)(4); Nemotron re-plans at 0.5 m/s; ACT, auto-executed; the AGV moves | 30–90 s (two Nemotron calls) |
| 1b | *Part shortage · F30* again, then the **Flow** tab with *compare* ticked | Two incidents, two routes: the second goes memory → playbook, with no planner call and no critic rejection | 10–30 s |
| 2 | Switch to **Supervised**, then *Part shortage · F30* again | "Waiting for you", and nothing moves. Press **Approve & dispatch** and it moves. Switch back to **Auto** | 10–60 s |
| 3 | *AGV-3 fault + stall* | AGV-3 is fenced; the replacement AGV stalls; the verifier re-plans with another AGV | 30–90 s |
| 4 | *Overheat · F10 (false alarm)* | The monitor sees the sensors disagree, so VERIFY: "Waiting for you" (a technician would enter the machine area). **Approve** | 10–60 s |
| 5 | **Lab** tab, *Run experiment* | Nemotron proposes a policy; 20 replays and a CI bar; *Save proposal to Zetaris* | 20–60 s |
| 6 | **Ask the floor** tab, *Why does F10 keep stopping?* (or type your own question) | Coordinator → SQL agents on Zetaris → log agent → analyst → critic; the answer cites E1–E4 with the SQL and rows | 60–200 s live (4–6 Nemotron calls); instant in sample mode |
| 7 | **Report your own** under the map: e.g. *nutrunner torque fault* at T20, severity high, a note | Fenced, then ESCALATE with the reason (no playbook yet), or the full loop for a planned kind | instant to 90 s |
| 8 | **What the agents know** | World Model fleet (AGV-3 overdue), certificates (a lapsed forklift card), OSHA rules, H-MEM lessons | instant |

The plain-English line above the trace explains each step as it happens, so you can read it out loud.

**Tips:**
- Nemotron can take up to a minute per plan. Narrate the trace while you wait.
- To show the critic loop again after the system has learned the lesson, run the CLI with `--no-memory`, or
  restart the server: in-UI memory is per session and file-backed in `data/memory/`. Delete
  `data/memory/playbooks.json` before the demo for a fresh start.

## 3. If something goes wrong
| Symptom | Fix |
|---|---|
| `Port … is already in use` | A Windows app holds that port. `start_live.sh` picks a free one; by hand, use `python -m app.server --port 8120` |
| `No such container: shopfloor-pg` | Run `bash scripts/start_live.sh`; it recreates and reloads everything. Docker pruning can remove the container and its volume |
| Health: nightshift down or breaker OPEN | Start Nightshift. Meanwhile the agents run in safe mode (rule-based plan plus ESCALATE) |
| Anything fails just before the demo | Fall back to the offline demo: `SAMPLE_MODE=true .venv/bin/python -m app.server --port 8120`. It uses the recorded Nemotron replies and needs no network; say so on screen |
