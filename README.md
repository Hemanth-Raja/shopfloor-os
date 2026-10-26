<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/hero-dark.png">
  <img alt="Shop-Floor OS. AI agents plan. Robots execute. Humans judge. A multi-agent incident-response and AGV-dispatch system for a car final-assembly hall: agents read the plant through Zetaris, reason with NVIDIA Nemotron through Meterless Nightshift and remember with Meterless engines; code checks every plan against OSHA rules and simulates it before anything moves" src="docs/hero.png" width="100%">
</picture>

[![Track](https://img.shields.io/badge/Open%20Agent%20Hackathon%202026-Track%203%3A%20Reasoning%20Architecture-1d3550)](#the-problem-and-our-solution)
[![Tests](https://img.shields.io/badge/tests-313%20passing-2f6f4a)](tests/)
[![Chaos](https://img.shields.io/badge/chaos%20scenarios-15%2F15-2f6f4a)](docs/chaos.md)
[![Unsafe plans executed](https://img.shields.io/badge/unsafe%20plans%20executed-0-2f6f4a)](docs/eval.md)
[![Nightshift](https://img.shields.io/badge/Nightshift%20receipts-6%2C034%2F6%2C034%20verified-2f6f4a)](#meterless-nightshift-and-nvidia-nemotron)
[![License](https://img.shields.io/badge/license-Apache--2.0-555)](LICENSE)

**[▶ Demo video (3:56, live)](https://youtu.be/YExRZBpAkTA)** · **[Slide deck](https://drive.google.com/file/d/1JJXHiZ3pP-_qGZy0m5hewZ2OqyfJNHsD/view?usp=sharing)** · **[Project site](https://hemanth-raja.github.io/shopfloor-os/)** · **[Replays](https://hemanth-raja.github.io/shopfloor-os/replay.html)** · **[Evaluation](docs/eval.md)** ·
**[Chaos tests](docs/chaos.md)** · **[Production review](docs/production-readiness.md)** ·
**[Live demo runbook](docs/live-demo.md)**

**Run it in one line, no keys:** `SAMPLE_MODE=true python run.py run input_examples/example_1.json`

</div>

## Demo video
[Watch the 3:56 demo on YouTube](https://youtu.be/YExRZBpAkTA): recorded live on Zetaris, Meterless Nightshift and NVIDIA Nemotron.

[![Shop-Floor OS demo video: the safety critic rejects Nemotron's plan, recorded live](https://img.youtube.com/vi/YExRZBpAkTA/maxresdefault.jpg)](https://youtu.be/YExRZBpAkTA)

Backup copy: [Google Drive](https://drive.google.com/file/d/1-isRSBLG9rpX0y3aT5ojEByiOeNniJoG/view?usp=sharing) (same video, English captions).

Slide deck (PDF): [Google Drive](https://drive.google.com/file/d/1JJXHiZ3pP-_qGZy0m5hewZ2OqyfJNHsD/view?usp=sharing) · [in this repo](docs/OAH2026-Spartans-Shop-Floor-OS-deck.pdf)

## See it work
![Shop-Floor OS running in sample mode: an AGV fault with a stall is fenced, planned, checked and dispatched; the verifier sees the AGV stall and re-plans; the agent flow graph shows every hop and who chose it; Ask the floor answers "Why does F10 keep stopping?" with cited evidence from Zetaris](docs/demo.gif)

<sub>Recorded from the real app in sample mode (no keys): an AGV fault with a mid-route stall is fenced, planned, checked
and dispatched; the verifier sees the stall and re-plans; the agent flow shows every hop and who chose it; then
*Ask the floor* answers a question with cited evidence.</sub>

## The problem and our solution
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/problem-solution-dark.png">
  <img alt="The problem: every stopped minute costs cars (one every 75 s); the facts are scattered over four systems; about a quarter of alarms are noise; acting can hurt someone. Our solution: one view of the plant through Zetaris and the World Model; agents that reason and route with three Nemotron models; code decides what is safe (OSHA critic, dry run, gate, a person approves risk); it learns and proves it (H-MEM, the Lab, signed receipts). Measured: 0 unsafe plans executed, 216 of 216 planted unsafe plans caught, false alarms 235 to 19, 15 of 15 chaos scenarios, 6 of 6 rule-breaking first plans fixed" src="docs/problem-solution.png" width="100%">
</picture>

**The problem.** When something goes wrong at a station (a kit does not arrive, an AGV stalls, coolant spills, a
nutrunner reports a bad torque, a weld robot faults) the line stops, and some of these events put people at risk. The
facts a supervisor needs sit in four systems, and in seconds they must decide who to send, which robot to re-route,
whether the alarm is real, whether acting is safe under OSHA rules and the plant's SOPs, and whether to stop the line.

**Our solution.** Shop-Floor OS turns that into a decision that is grounded, checked, simulated and verified. Agents read
one federated view of the plant, Nemotron plans and routes, deterministic code decides what is safe, a person
approves anything risky, and the system learns from every outcome.

**Who it is for:** shift supervisors, team leaders and maintenance planners in vehicle assembly.
**Why it fits Track 3:** it turns data (Zetaris), knowledge (World Model, OSHA clauses), memory (H-MEM) and
reasoning (Nemotron) into decisions and actions that are checked, simulated, gated and verified.

## Results at a glance
| Area | Result |
|---|---|
| **The rule that matters** | Nemotron proposes; code decides what is safe. Every plan passes a deterministic OSHA critic, a simulated dry run and a restraint gate, and a person approves anything that sends someone into a hazard |
| **Safety, measured** | 0 unsafe plans executed. The critic caught 216 of 216 planted unsafe plans and wrongly rejected 0 of 54 safe ones; live, Nemotron's first plan broke a rule in 6 of 27 calls and fixed all 6 after the critic's feedback ([docs/eval.md](docs/eval.md)) |
| **Fewer false dispatches** | The Monitor sent 19 false alarms to the floor where a plain threshold alarm sends 235 (600 noisy alarms, Brier 0.096 vs 0.392) |
| **Failure-tested** | 15 chaos scenarios (a model down, Nightshift down, 60% of calls failing, broken JSON, a different model answering, Zetaris down…) plus a 19-incident shift on flaky models: every one ends in a decision, nothing unsafe runs ([docs/chaos.md](docs/chaos.md)) |
| **Every model call, signed** | Three NVIDIA Nemotron models, each pinned to its own Nightshift app; every call records the requested and the answering model, and 6,215 of 6,215 Ed25519 receipts verify (`run.py receipts --verify`, 26 Oct 2026) |
| **Data** | Four sources federated by Zetaris and modelled as one Unified Semantic Layer; the agents read it on their own read-only login, so a write from an agent is refused by Zetaris itself |
| **Runs without us** | `SAMPLE_MODE=true` runs the real agent loop offline, with no keys, Zetaris or Nightshift |

## How it works
Four agent teams share one data layer, one model gateway and one set of deterministic safety checks.

![Architecture: people use four agent teams (incident response, autopilot, Ask the floor, Lab in the loop); the teams use the Meterless engines, every plan goes through deterministic safety code before the simulated floor moves, data comes from Zetaris and every model call goes through Meterless Nightshift to NVIDIA Nemotron](docs/architecture.png)

<sub>Source: [docs/architecture.html](docs/architecture.html).</sub>

**One incident, start to finish.** There is no fixed pipeline: an orchestrator agent picks the next agent at
each step, but only among the moves that code allows (details in
[How an incident is handled](docs/agents.md#how-an-incident-is-handled-a-dynamic-orchestrator)).

```mermaid
flowchart TD
  S[Signal or report] --> R[Reflex: fence the area,<br/>stop AGVs, under 100 ms]
  R --> M{Monitor: real<br/>or sensor noise?}
  M -- "noise" --> V[Check first: one qualified<br/>person inspects, line keeps running]
  M -- "real" --> C[Candidates from the World Model:<br/>one federated Zetaris query]
  H[(H-MEM lessons<br/>and playbooks)] --> C
  C -- "proven playbook fits" --> PB[Reuse the playbook,<br/>no model call]
  C --> P1[Plan v1 at once:<br/>optimal assignment]
  C --> P2[Plan v2 in the background:<br/>Nemotron, strict JSON, cites clauses]
  P1 --> K{Safety critic:<br/>OSHA and SOP rules}
  P2 --> K
  PB --> K
  K -- "rejected: the clause goes back,<br/>up to 3 tries" --> P2
  K -- "passed" --> D{Dry run in the sim:<br/>collisions, crossings, ETA}
  D -- "passes" --> RF[Refine: Nemotron's v2 replaces v1 only<br/>if it passes both checks and scores better]
  D -- "fails" --> G
  RF --> G{Restraint and gate}
  G -- "routine ACT" --> E[Executor dispatches<br/>as VDA 5050 orders]
  G -- "hazard, lapsed certificate,<br/>failed dry run, ESCALATE" --> U{Person approves<br/>or rejects}
  U -- "approve" --> E
  U -- "reject" --> X[Area stays fenced;<br/>nothing moves]
  E --> VF{Verifier: did the floor<br/>change as predicted?}
  VF -- "no, e.g. AGV stalled:<br/>re-plan without it, up to 3 rounds" --> C
  VF -- "yes" --> L[Done]
  L -- "lesson, outcome" --> H
```

## Try it in 2 minutes
No accounts, keys, Zetaris or Nightshift needed.

1. **One incident, JSON in and out.**
   ```bash
   python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
   SAMPLE_MODE=true .venv/bin/python run.py run input_examples/example_1.json --out result.json
   ```
   stdout shows one `[INFO]` trace line per agent step, then the JSON result; `--out` also writes the JSON alone
   to `result.json`. A bad request returns `"status": "error"` with a message, and exit code 1.
   A part shortage at F30. The rules plan is ready at once; Nemotron's recorded plan sends AGV-5 through a
   pedestrian crossing at 1.0 m/s, the critic rejects it under 29 CFR 1910.178(n)(4), and the safe plan runs.
2. **The whole floor in a browser.** `SAMPLE_MODE=true .venv/bin/python -m app.server`, then open
   http://127.0.0.1:8000. Report an incident under the map, watch the agents in *Incidents*, ask
   "Why does F10 keep stopping?" in *Ask the floor*, and run an experiment in *Lab*.
   With Docker: `docker build -t shopfloor-os . && docker run --rm -p 8000:8000 shopfloor-os`.
3. **The judges' API check.**
   ```bash
   curl -X POST http://localhost:8000/run -H "Content-Type: application/json" -d @input_examples/example_3.json
   ```
   An overheat at F10 whose vibration evidence says sensor noise: the answer is VERIFY, and the line keeps running.
4. **Re-check a decision without any model.** Report an incident in the UI, then open
   `http://127.0.0.1:8000/api/verify/SIM-42-001` (live: `.venv/bin/python run.py verify SIM-42-003`). Code alone
   re-derives the decision from the saved trace: the plan passes the critic again, a plan had passed before
   dispatch, the human gate was respected, the plant data is the same snapshot, and the hash-chained journal holds
   the same outcome.

| Question | Where it is answered |
|---|---|
| What happens when it fails? | [Retrieval, context limits and failure handling](docs/reliability.md#retrieval-context-limits-and-failure-handling), [Resilience](docs/reliability.md#resilience-what-happens-when-things-go-wrong), [docs/chaos.md](docs/chaos.md) |
| What does it do without being asked? | [Autopilot](docs/agents.md#autopilot-a-whole-shift-many-incidents-at-once) and [Autonomy levels](#autonomy-levels) |
| How can a person tell afterwards what it did? | per-run traces in [`logs/`](logs), the *Incidents* tab, `run.py verify`, `run.py audit`, `run.py handover` |
| Which model answered each call? | `logs/llm.jsonl` (requested and answering model), the *Nemotron at work* tab, `run.py receipts --verify` |
| Does the model add anything, and is it safe alone? | [docs/eval.md](docs/eval.md) sections 3 and 4: Nemotron alone vs the full loop |

## Setup
### From a clean clone, without Zetaris, Nightshift or keys
```
git clone <this repo> && cd shopfloor-os
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env                       # an empty .env is fine for sample mode
SAMPLE_MODE=true .venv/bin/python run.py run input_examples/example_1.json
SAMPLE_MODE=true .venv/bin/python -m app.server       # web UI on http://127.0.0.1:8000
.venv/bin/python -m pytest -q                          # 313 offline tests
```
With Docker: `docker build -t shopfloor-os .` then `docker run --rm -p 8000:8000 shopfloor-os`. The image is
tested (176 MB) and defaults to `SAMPLE_MODE=true`.

`SAMPLE_MODE=true` still runs the real agent loop on the offline plant: simulator, A*, critic, dry-run,
restraint gate, executor and verifier. Recorded Nemotron replies ([`logs/fixtures/`](logs/fixtures)) are used where one exists
for that exact input. Otherwise a deterministic rule-based planner stands in, and it is still checked by the
critic. No output is read from [`output_examples/`](output_examples).

<details>
<summary><b>Live setup, with Zetaris and Meterless Nightshift</b></summary>

1. Install and start Zetaris (Docker) and Meterless Nightshift as described in the Participant Setup Guide and
   [zetaris.github.io/getting-started](https://zetaris.github.io/getting-started/).
2. Put the Zetaris REST URL, org id and API key, and the Nightshift base URL and runtime token, into `.env`
   (see [`.env.example`](.env.example)). Choose a Postgres password for `PG_PASSWORD`. The NVIDIA key stays in Nightshift.
3. Load the data and register it in Zetaris (fully scripted, no manual steps):
```
bash scripts/start_postgres.sh          # Postgres container on the Zetaris Docker network
bash scripts/load_data.sh               # generate the plant from data/seed and load fleet, hr, plant, ops
.venv/bin/python -m app.zetaris register     # first time: create the sf_* Zetaris datasources
.venv/bin/python -m app.zetaris refresh      # after any reload
.venv/bin/python run.py health               # Zetaris and Nightshift through their circuit breakers
```
4. Optional, recommended: the agents' read-only login and the Unified Semantic Layer.
```
.venv/bin/python -m app.zetaris reader-role  # role SF_AGENT_READER: SELECT on the four sf_* datasources only
.venv/bin/python -m app.usl deploy           # USL 'floor' (9 tables with keys) over the sf_* sources
.venv/bin/python -m app.usl status           # its tables and activation flags
```
   Create a non-admin Zetaris user with the role `SF_AGENT_READER` and put its API key in `.env` as
   `ZETARIS_AGENT_API_KEY`; agent reads then use it and Zetaris refuses any write from them. Zetaris only exposes
   the USL access lists in its GUI, so give the role access there once: Management → Permissions (View on Unified
   Semantic Layer), and Data Product → Unified Semantic Layer → `shopfloor` → Access Control, then the `floor`
   model → Access Control. Until then the agents read the raw federated query instead, and say so in the logs.
   A redeploy of the USL empties those lists.

In Docker, use `host.docker.internal` instead of `localhost` for Zetaris and Nightshift.

Or do steps 3 onwards in one go: `bash scripts/start_live.sh` (idempotent; it starts the UI on a free port). The live demo runbook is [`docs/live-demo.md`](docs/live-demo.md).

</details>

## Usage
| Entry point | Command |
|---|---|
| **JSON in, JSON out** (programmatic) | `.venv/bin/python run.py run input_examples/example_2.json` |
| **HTTP API** | `POST /run` or `POST /api/run` with the same JSON body (`python -m app.server`, port 8000); `GET /health` |
| **Web UI** | `python -m app.server`: *Live floor*, *Incidents*, *Ask the floor*, *Lab*, *Nemotron at work* and *What the agents know* tabs, Approve/Reject |
| One incident (CLI trace) | `run.py incident --kind agv_fault --agv AGV-3 --station F30 --stall-after 2` |
| Zone briefing | `run.py context --zone FINAL` (World Model built from one federated Zetaris query, plus a Nemotron summary) |
| **Ask the floor** (investigation) | `run.py ask "Why does F10 keep stopping?"` or the *Ask the floor* tab; `--json` prints the full result |
| Custom incident | the *Report your own* form under the map (kind, station, severity, note), or `input.severity` / `input.note` in the JSON contract |
| **Autopilot shift** | `run.py shift --minutes 8 --seed 7 --rate 2.5` or *Run shift* under the map: Sentinel, concurrent incidents, leases, summary |
| **Always on** | `python -m app.server --always-on` (or `ALWAYS_ON=true`): the Sentinel starts with the server and shifts follow each other until you press *Stop*; in the UI, *keep watching* next to *Run shift* |
| Lab experiment | `run.py lab [--write]` (hypothesis, then 20 seeded replays, then bootstrap CI, then an `sf_ops.experiments` proposal) |
| **Shift handover** | `run.py handover [--shifts 3]`: the shift journal read in bounded chunks on the Meterless Markovian engine, then a handover note |
| **Evaluation** | `run.py eval [--live]`: planted unsafe plans, random plans, Monitor calibration; `--live` adds a Nemotron-only baseline and 3x stability. Writes [docs/eval.md](docs/eval.md) |
| Project site and replays | [hemanth-raja.github.io/shopfloor-os](https://hemanth-raja.github.io/shopfloor-os/): landing page [`docs/site/index.html`](docs/site/index.html); `python scripts/build_site.py` rebuilds the replay page (`docs/site/replay.html`) from the examples; [`.github/workflows/pages.yml`](.github/workflows/pages.yml) publishes both on push |

<details>
<summary><b>Request and response format</b></summary>

Request:
```json
{"run_id": "example-2",
 "input": {"kind": "agv_fault", "agv": "AGV-3", "station": "F30", "stall_after": 2, "seed": 42},
 "options": {"sample_mode": false, "memory": false}}
```
- `input.kind` is one of `part_shortage`, `agv_fault`, `overheat`, `spill`, `robot_cell_fault`, `torque_fault`,
  `near_miss` or `quality_defect`. All eight run through the full loop. A kind without a response playbook would be
  fenced and escalated to a supervisor with the reason, instead of being planned.
- Optional `severity` (`low`, `medium`, `high`, `critical`) and `note` (free text from the reporter).
- `station` is a station id (B10, B20, P10, T10, T20, M10, F10, F20, F30, E10).
- `false_alarm` makes an overheat sensor noise.
- `stall_after` stalls the dispatched AGV mid-route.

Or over HTTP (`SAMPLE_MODE=true python -m app.server`, or the Docker image):
```bash
curl -X POST http://localhost:8000/run -H "Content-Type: application/json" -d @input_examples/example_1.json
```

Response (abridged from `example_1`):
```json
{"run_id": "example-1", "status": "success",
 "output": {"decision": "ACT", "score": 0.942,
            "summary": "part shortage at F30: ACT, Optimal assignment (Hungarian) over 5 candidates for 2 role(s) ...",
            "reasons": ["p_real=0.95", "sensors agree=True", "expected cost of waiting $5,082 vs acting $42"],
            "plan": {"assignee_ids": ["worker:plant:W-18", "agv:plant:AGV-5"],
                     "agv_routes": [{"agv_id": "agv:plant:AGV-5", "to_station": "F30", "speed_mps": 1.0,
                                     "crossing_speed_mps": 0.5}]},
            "citations": [{"ref": "SOP-KIT-01 §3"}, {"ref": "29 CFR 1910.178(n)(8)"}],
            "critic_rejections": 1, "gate": "auto_executed", "collisions": 0,
            "path": [{"hop": 0, "agent": "reflex", "by": "code"}, {"hop": 1, "agent": "monitor", "by": "forced"}, "..."]},
 "models": ["nvidia/nemotron-3.5-lightning-30b-a3b", "nvidia/nemotron-3.5-super-vl-120b-a12b", "rules (Hungarian)"],
 "trace_id": "trace-example-1", "log_file": "logs/trace-example-1.jsonl", "execution_time_seconds": 0.03}
```

The response contains:
- `status`;
- `output`: decision, score, reasons, plan, citations, critic_rejections, replans, safe_mode, collisions;
- `agents`, with their roles;
- `models`, naming which model handled each request;
- `trace_id`, `log_file` and `execution_time_seconds`.

Failures return `status: "error"` with a typed `error` (`InvalidInput`, `UpstreamUnavailable`, and so on) rather
than a crash.

</details>

### Sample inputs and outputs
| Input | What happens | Output |
|---|---|---|
| [`input_examples/example_1.json`](input_examples/example_1.json): part shortage at F30 | The rules plan (v1, optimal assignment) is ready at once. Nemotron's plan (v2) sends AGV-5 at 1.0 m/s through the X-WEST crossing; the critic rejects it under 29 CFR 1910.178(n)(4), so v1 (0.5 m/s at crossings) stays. Result: ACT | [`output_examples/example_1.json`](output_examples/example_1.json) |
| [`input_examples/example_2.json`](input_examples/example_2.json): AGV-3 fault plus a stall | The replacement AGV-1 stalls mid-route. The verifier detects it and the loop re-plans with AGV-2. Result: ACT | [`output_examples/example_2.json`](output_examples/example_2.json) |
| [`input_examples/example_3.json`](input_examples/example_3.json): overheat at F10, false alarm | The temperature is over its threshold, but the Bosch vibration recording is normal, so the sensors disagree. Result: VERIFY (a technician checks; the line keeps running) | [`output_examples/example_3.json`](output_examples/example_3.json) |
| [`input_examples/example_4.json`](input_examples/example_4.json): coolant spill at F20 | A two-cell geofence goes up and AGVs stay out. The spill responder goes in, citing 29 CFR 1910.22. Result: ACT | [`output_examples/example_4.json`](output_examples/example_4.json) |
| [`input_examples/example_5.json`](input_examples/example_5.json): robot-cell fault at B10 (weld robots) | The cell is fenced. Only a technician with in-date lockout/tagout and a robot-cell card may enter (29 CFR 1910.147, SOP-ROBOT-02). ACT, but it waits for a person to approve | [`output_examples/example_5.json`](output_examples/example_5.json) |
| [`input_examples/example_6.json`](input_examples/example_6.json): nutrunner torque fault at F10 | v1 sends a tool technician and the torque auditor. Nemotron's v2 sends only the technician; the critic rejects it (SOP-TORQUE-01: joints since the last good torque must be re-audited), so v1 stays. A single NOK with `false_alarm` gives VERIFY | [`output_examples/example_6.json`](output_examples/example_6.json) |
| [`input_examples/example_7.json`](input_examples/example_7.json): pedestrian near miss at F30 | The AGV involved is held where it stopped; a supervisor with incident command secures the scene. A safety event, so a person signs off | [`output_examples/example_7.json`](output_examples/example_7.json) |
| [`input_examples/example_8.json`](input_examples/example_8.json): quality defect at end-of-line | A quality inspector contains the affected units (SOP-QUAL-01). Routine, auto-executed | [`output_examples/example_8.json`](output_examples/example_8.json) |

<details>
<summary><b>Logs and traces: what every run writes, with a real excerpt</b></summary>

**Watch it live:**
- **Incidents tab** in the web UI: every incident, running ones first, with its agent flow drawn as a graph (numbered hops, who chose each hop and why) and its full history: each agent's input and output, what was sent to Nemotron, the model that answered and the latency, approvals and timeouts.
- **`python run.py watch`** in a second terminal: one coloured, plain-English line per agent step, Nemotron call and human judgement, for example `12:03:04 PLANNER  1.2s  Nemotron-Super attempt 2 -> send W-18; AGV-5->F30 1.0/0.5 m/s`.

Every run writes `logs/trace-<run_id>.jsonl`, one JSON object per agent step. Each record has the agent, ok,
timing, inputs and outputs, critic violations with clauses, the model that answered, and retries and re-plans.
Every record also carries the standard fields `trace_id`, `agent_name`, `action`, `input_summary`,
`output_summary`, `from_agent`, `target_agent`, `confidence`, `retry_count` and `status`
([`app/tracefmt.py`](app/tracefmt.py)), and every step is printed as an `[INFO]` line on stdout. From
`example_1`, the critic stopping Nemotron's plan:
```jsonl
{"trace_id": "trace-example-1", "agent_name": "planner", "action": "write_plan", "input_summary": "part_shortage at F30", "output_summary": "v1 plan: send W-18, AGV-5; AGVs AGV-5->F30; cites 2 clause(s)", "from_agent": "candidates", "target_agent": "critic", "confidence": null, "retry_count": 0, "status": "success"}
{"trace_id": "trace-example-1", "agent_name": "critic", "action": "check_safety", "input_summary": "part_shortage at F30; checks the nemotron v2 plan", "output_summary": "v2 rejected: R-CROSSING; the plan goes back with the clauses", "from_agent": "dryrun", "target_agent": "refine", "confidence": null, "retry_count": 0, "status": "needs_revision"}
{"trace_id": "trace-example-1", "agent_name": "refine", "action": "compare_plans", "input_summary": "part_shortage at F30", "output_summary": "kept v1: v2 broke R-CROSSING", "from_agent": "critic", "target_agent": "restraint", "confidence": null, "retry_count": 0, "status": "success"}
{"trace_id": "trace-example-1", "agent_name": "restraint", "action": "decide", "input_summary": "part_shortage at F30", "output_summary": "ACT (score 0.942)", "from_agent": "refine", "target_agent": "gate", "confidence": 0.942, "retry_count": 0, "status": "success"}
```

Other logs:
- `logs/llm.jsonl` records every Nightshift request with the requested and answering model and the latency.
- `logs/world_model.jsonl` records World Model builds.
- `logs/approvals.jsonl` records human judgements.

None of them contain secrets.

A real live-mode trace: [docs/live-run/](docs/live-run/README.md) holds incident `SIM-42-008` (part shortage at F30,
the critic rejecting Nemotron Ultra's v2 plan) as recorded live on 26 Oct 2026, with its four Nightshift calls and receipts.

</details>

## Agents
| Agent | Role | Model | Can block or retry? |
|---|---|---|---|
| reflex | Geofences the incident and stops or re-routes AGVs, in under 100 ms | none (rules) | Acts first; cannot be skipped |
| monitor | Real alarm or sensor noise, from thresholds plus Bosch vibration evidence; one-line summary | `monitor` role (summary only) | Yes: noise sends one person to check instead of a dispatch |
| memory | Recalls H-MEM lessons from past incidents before planning | none (H-MEM ranking) | No |
| candidates | Top-3 qualified workers and healthy AGVs from the World Model (valid certifications; no service-overdue AGV for critical work) | none | Yes: only certified people and healthy AGVs get through |
| planner | Chooses among the candidates; strict JSON; must cite the governing clauses | `planner` role | Retries up to 3 times with the critic's clauses |
| safety critic | Deterministic OSHA-based rule engine; cites the exact clause per violation; the planner gets up to 3 tries. Rules include lockout/tagout and robot-cell entry (robot-cell faults, overheats), torque re-audit (torque faults), the held AGV and supervisor sign-off (near misses) and inspector containment (quality defects) | none | Yes: blocks any plan that breaks a rule, with the clause |
| dry-run | 60 s headless simulation with A* and a reservation table; fails on a collision or a late ETA | none | Yes: a collision or late ETA sends the plan back |
| restraint | ACT, VERIFY, SAFE_HOLD, ESCALATE or DO_NOTHING, from confidence, sensor agreement and cost | none | Yes: can hold, verify or escalate instead of acting |
| executor / verifier | Dispatches in the sim and checks the world changed as predicted; re-plans on mismatch | none | Yes: a mismatch triggers a re-plan |
| health | Circuit breakers for Zetaris and Nightshift; safe mode when Nightshift is down | none | Yes: opens breakers and switches to safe mode |
| lab | Policy hypothesis, then 20 seeded replays (no LLM), then a bootstrap 95% CI | `lab` role (hypothesis only) | Only proposes; a person applies |
| orchestrator | Chooses the next agent at each decision point, within the legal moves (see below) | `router` role | Routes only among legal moves; code overrides an illegal pick |

## What makes this an agent system
| Pattern | Where it happens |
|---|---|
| **Dynamic delegation** | The orchestrator picks the next agent hop by hop; the router model chooses only among the moves code allows, so incidents take different routes ([`output_examples/`](output_examples)) |
| **Critique loop** | The safety critic rejects a plan with the OSHA or SOP clause; the planner gets the clause and tries again (up to 3 times); the evidence critic drops any claim the cited rows do not back |
| **Retry logic** | Model calls retry with backoff, then the next model; a refused SQL query goes back with the guard's reason for one retry; a stalled AGV makes the verifier re-plan |
| **Shared state** | All agents read and write one incident state and the Meterless World Model built from one federated Zetaris query; H-MEM is shared memory across incidents |
| **Escalation** | No playbook, no qualified person, a lapsed certificate or a failed model chain ends in ESCALATE or SAFE_HOLD, and a person decides |
| **Role authority** | The critic, dry run and human gate can block dispatch; nothing reaches the floor without passing all three |
| **Non-linear flow** | Branches on alarm confidence (VERIFY vs ACT), risk (human gate), resources taken by another incident (arbiter) and the world not changing as predicted (re-plan) |

**Where to look.** Agent decisions: `output.path` in every response, with who chose each hop and why. Zetaris:
[`app/zetaris.py`](app/zetaris.py) and the `sql` and `candidates` steps. Meterless: [`app/engines/`](app/engines) and
the `memory`, `candidates` and handover steps. Retries and feedback: `status: needs_revision`, `retry_count` and
`replans` in the trace. Fallback to sample data: `SAMPLE_MODE=true`. Logs: [`logs/`](logs) and stdout. The final
answer: the `restraint` and `gate` steps, returned as `output.decision`.

## Lab in the loop: test a floor change, apply it, measure it
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/loop-dark.png">
  <img alt="The loop, read clockwise: the model (Nemotron via Nightshift with H-MEM memory) proposes plans and one policy change at a time into the complex space of the assembly hall (the World Model: stations, AGVs, people, certificates, OSHA rules, 2,071 past incidents); the agents try each plan on the digital twin before acting (collisions, crossings, who arrives when); what worked goes to the Lab in the loop (20 seeded replays of real history, 95% CI, you save what works); lessons and test results flow back to the model. In the middle, a self-improving search: unsafe plans rejected with the clause and fixed (6 of 27 first plans, all 6 fixed), lessons gain or lose confidence, false dispatches 235 to 19" src="docs/loop.png">
</picture>

<sub>Read it clockwise. A model proposes, the agents try the idea on a digital twin of the hall before anything real
moves, the Lab tests changes against history, and what was learned flows back to the model. A person sits on two
edges: approving risky plans before the twin's result reaches the floor, and saving a Lab proposal. Source:
[docs/loop.html](docs/loop.html).</sub>

The incident loop learns from each incident (H-MEM lessons). The Lab asks a slower question: would changing the
floor itself make a whole class of incidents shorter?

![Two learning loops. Fast loop, every incident: the handled incident leaves an H-MEM lesson whose confidence goes up or down, and the next plan recalls it. Slow loop, Lab in the loop: incidents are logged as history in Zetaris, Nemotron proposes one change, code tests it on history with A* re-scoring and 20 seeded replays, a person saves it to sf_ops.experiments and applies it, and new incidents measure it](docs/learning-loops.png)

<sub>Source: [docs/learning-loops.html](docs/learning-loops.html).</sub>

<details>
<summary><b>How one experiment runs</b></summary>

One *Run experiment* (`run.py lab`, the *Lab* tab):
1. **Reads history.** One Zetaris query summarises past spills and part shortages per station: count, mean time to
   clear, and the walking distance to the nearest spill kit or to the parts supermarket.
2. **Proposes a change.** Nemotron (the `lab` role) reads the summary, plus the runs already tested and what an
   applied change measured, and proposes one testable change: a spill kit at a station, or a loaded AGV waiting at
   one. With Nemotron down, a rule picks the busiest station, and the page says so in red.
3. **Re-scores history.** Every real incident of that kind is re-walked with A* on the floor grid, with and without
   the change; only the travel part of its clear time changes.
4. **Replays and measures.** 20 seeded replays and 2,000 bootstrap resamples give the change in mean time to
   clear with a 95% interval. No model runs inside the replays, so the same run gives the same answer.
5. **Apply and measure.** A significant result can be saved to Zetaris (`sf_ops.experiments`) and applied to the
   floor: the map shows it, and every later incident of that kind is measured with and without it
   ([`app/lab/loop.py`](app/lab/loop.py)), predicted saving next to measured. *Remove the change* takes it off; both survive a restart.

</details>

## Sponsor technology
### Zetaris: the data layer
Every read the agents make goes through Zetaris ([`app/zetaris.py`](app/zetaris.py), the only data access layer), and every Zetaris
object was created in code through its SQL REST API, so a fresh install is rebuilt by commands, not by clicks.
- **Four sources, one query.** Four Postgres databases are registered as the datasources `sf_fleet`, `sf_hr`,
  `sf_plant` and `sf_ops` (`python -m app.zetaris register`). Every incident starts with **one federated Lightning
  SQL query** across all four ([`app/engines/plant_world.py`](app/engines/plant_world.py)): stations, machines, workers with their
  certifications, AGVs with live position, safety rules and cost history.
- **Unified Semantic Layer.** [`app/usl.py`](app/usl.py) defines the USL `floor` (9 business entities with primary and foreign
  keys: zone, station, machine, worker, certification, agv, vibration_sample, safety_rule, incident) over the raw
  sources, and deploys and activates it. `run.py context` reads through it, and falls back to the raw federated
  query if the USL is unavailable (the World Model log records which one was used).
- **Read-only agents.** The agents read on their own Zetaris login whose only role, `SF_AGENT_READER`, has SELECT on
  the four datasources and the USL. A write from an agent is refused by Zetaris itself, on top of our SQL guard
  (one read-only statement, whitelisted `sf_*` tables, capped `LIMIT`). The operator login is kept for register,
  refresh and the Lab's write.
- **Investigations.** *Ask the floor*'s SQL agent writes Spark SQL from the table catalogue and runs it on Zetaris;
  the investigator and the Lab read incident history the same way.
- **Writes back.** Lab proposals go back through Zetaris (`INSERT INTO sf_ops.experiments`), and the agents' own
  logs are synced every 30 s into `sf_ops.agent_steps`, `sf_ops.llm_calls` and `sf_ops.journal`, so the agents can
  be questioned about their own behaviour with the same SQL as the plant.

### Meterless engines
Three engines are implemented in Python from the Meterless specs and each runs a feature, with its conformance tests:
- **World Model** ([`app/engines/world_model.py`](app/engines/world_model.py)) grounds every plan: stable ids, provenance, facts such as
  `service_overdue`, and `ground()`, which rejects ids that do not exist. [`tests/test_world_model.py`](tests/test_world_model.py) ports the
  spec's 8 conformance checks.
- **H-MEM** ([`app/engines/hmem.py`](app/engines/hmem.py)) holds the incident playbooks, the critic's lessons with outcome feedback, and
  the OSHA/SOP clause index that the planner and critic cite. [`tests/test_hmem.py`](tests/test_hmem.py) runs all 13 official vectors.
- **Markovian** ([`app/engines/markovian.py`](app/engines/markovian.py)) writes the shift handover (`run.py handover`) by reading a whole
  shift's journal in bounded chunks with compressed carryover: 67% fewer prompt tokens on a live three-shift run.
  [`tests/test_markovian.py`](tests/test_markovian.py) covers it.

Run the three conformance suites (no network, no Zetaris, no Nightshift):
```bash
.venv/bin/python -m pytest -q tests/test_world_model.py tests/test_hmem.py tests/test_markovian.py
```
Result on 26 Oct 2026: `39 passed in 0.06s`.

Details, including the conformance checks: [docs/models-and-engines.md](docs/models-and-engines.md).

### Meterless Nightshift and NVIDIA Nemotron
Nightshift is the only model provider: every call goes through [`app/llm.py`](app/llm.py) and [`app/models.py`](app/models.py) to
`MODEL_BASE_URL`, and the NVIDIA key stays inside Nightshift. Every model is an NVIDIA Nemotron model.
- **One app per model.** Nightshift routes by app name, so each model is called under its own app, pinned in
  Nightshift's app policy to exactly one lane: `shopfloor-os-ultra` → Nemotron 3 Ultra 550B, `shopfloor-os` →
  Nemotron 3.5 Super VL 120B, `shopfloor-os-fast` → Nemotron 3.5 Lightning 30B. A startup self-check asks each one
  who answers, and a role's chain skips any model that is not really served.
- **Proof per call.** Each call records the requested and the answering model and the id of Nightshift's signed
  receipt, and the receipt's Ed25519 signature is verified at the moment the answer is used. `run.py receipts
  --verify` re-checks all of them (6,215 of 6,215 on 26 Oct 2026).
- **NVIDIA's guidance applied.** Reasoning and the answer share the token budget, so reasoning is switched off
  for the small, frequent decisions (router, monitor, Sentinel triage) and set to medium for the coordinator and
  the SQL agent; the planner, analyst, Lab and handover keep the model's default reasoning with a larger budget.
- **Nemotron at work.** A tab and `GET /api/nemotron?hours=` count, from the logs and traces rather than from
  claims, what Nemotron decided: calls and tokens per role, which model answered, orchestrator hops chosen by
  Nemotron vs forced by code, plans it wrote, refinements kept or rejected and why, and receipts verified.

### Cursor
The project rules live in [`.cursor/rules/shopfloor.mdc`](.cursor/rules/shopfloor.mdc) and [`AGENTS.md`](AGENTS.md) (kept in sync). Cursor built the model
client ([`app/llm.py`](app/llm.py)) and its tests; Claude Code was also used (see *Disclosure*).

### What is unique
- **Three Meterless engines work together.** The World Model grounds ids and facts, H-MEM ranks both lessons and
  regulatory clauses, and Markovian reads a whole shift's journal in bounded chunks for the handover.
- **The model never acts unchecked.** Nemotron proposes and routes; deterministic code with clause citations
  decides what is safe, simulates it, gates it and verifies the result, and a decision can be re-derived later
  with no model at all (`/api/verify/<id>`).
- **Zetaris is also the agents' memory of themselves.** Their own steps, model calls and journal are federated
  next to the plant data, so "how often did the critic reject the planner, and why?" is one SQL query.

## Built with
| Component | Technology |
|---|---|
| Language | Python 3.11+, mostly the standard library |
| API and UI | stdlib `http.server` with server-sent events; `POST /run`; a vanilla-JS canvas UI |
| Orchestration | Our own dynamic orchestrator ([`app/dynamic.py`](app/dynamic.py)), no agent framework; Pydantic contracts with strict JSON between agents |
| Data access | Zetaris REST API (Lightning SQL), with a Unified Semantic Layer and a read-only agent login |
| Data stores | Four Postgres 16 databases behind Zetaris; an offline SQLite copy for sample mode |
| Model runtime | Meterless Nightshift (OpenAI-compatible), signed and verified receipts |
| Models | NVIDIA Nemotron 3 Ultra 550B, 3.5 Super VL 120B, 3.5 Lightning 30B |
| Memory and grounding | Meterless World Model, H-MEM and Markovian engines, ported to Python |
| Simulation | Deterministic 20 x 40 grid, space-time A* with a reservation table, seeded incidents |
| Logging | JSONL per run in [`logs/`](logs) plus `[INFO]` lines on stdout; hash-chained journal |
| Tests | pytest, 313 offline tests including the engines' conformance suites and property-based safety fuzzing |
| Container | Docker (`python:3.13-slim`, 176 MB, sample mode by default) |
| Development | Cursor and Claude Code |

<details>
<summary><b>Dependencies</b></summary>

- **Python 3.11+** and [`requirements.txt`](requirements.txt): openai (the OpenAI-compatible client for Nightshift), python-dotenv,
  requests, pydantic and pytest. The generator, simulator, engines and web server use only the standard library.
- **Live mode:** Zetaris (local Docker), Postgres 16 (Docker container `shopfloor-pg`, created by
  [`scripts/start_postgres.sh`](scripts/start_postgres.sh)) and Meterless Nightshift, with NVIDIA's hosted Nemotron models. No GPU is needed.
- **Optional rebuild of [`data/seed/`](data/seed):** [`requirements-data.txt`](requirements-data.txt) (h5py), used only by [`app/data/fetch_sources.py`](app/data/fetch_sources.py).

</details>

## Reliability and safety
### Sample mode
`SAMPLE_MODE=true` (or `"options": {"sample_mode": true}` in a request) runs the whole system with no keys, Zetaris
or Nightshift: the offline plant and an SQLite copy of the four sources are built from [`data/seed/`](data/seed),
recorded Nemotron replies ([`logs/fixtures/`](logs/fixtures)) are used where one exists for that exact input, and a
deterministic rule-based planner stands in otherwise. The agents still run for real: reflex, monitor, candidates,
critic, dry run, restraint, gate, executor and verifier all compute their results, so a new input gives a new
answer. No final answer is stored or read from [`output_examples/`](output_examples). Tested from a fresh clone with
an empty `.env`, and in the Docker image.

### Failure handling
| Failure | Behaviour |
|---|---|
| A model times out, errors or returns broken JSON | Retry with backoff, JSON repair, one re-ask, then the next Nemotron model in the role's chain |
| Every model is down (Nightshift unavailable) | Breakers open; rule-based router; safe mode writes a rules plan and ESCALATEs to a person |
| Zetaris is down | Its circuit breaker opens; a run returns a typed `UpstreamUnavailable` error that says to retry or use sample mode; in Ask the floor a failed query becomes failed evidence and any claim citing it is dropped |
| Unsafe or invalid SQL from the SQL agent | The guard refuses it (one read-only `SELECT` on `sf_*` tables); the reason goes back for one retry |
| A plan breaks a safety rule | The critic rejects it with the clause; the planner retries; a rule it cannot check blocks automatic action |
| The model names a person or robot that does not exist | World Model `ground()` rejects the id and the planner gets the reason |
| The floor does not change as predicted | The verifier detects it and the orchestrator re-plans |
| Malformed request | `status: "error"` with a typed `InvalidInput` message, exit code 1 on the command line |

All 15 failure scenarios are broken on purpose in [docs/chaos.md](docs/chaos.md); the full tables are in
[docs/reliability.md](docs/reliability.md).

### Autonomy levels
| Level | Setting | What runs by itself | What waits for a person |
|---|---|---|---|
| 0 | (always on) | The reflex: stop nearby AGVs, fence the area, move people without lockout/tagout out; the runtime monitor | Nothing; these only make the floor safer |
| 1 | Advisory | Detection, investigation and a recommended plan | Every dispatch; a person dispatches |
| 2 | Supervised | Planning, critic, dry run | Every dispatch is approved first |
| 3 | Auto (default) | A routine plan that passed the critic and the dry run is dispatched at once; a person can roll it back | A person entering a hazard, a lapsed certificate, an unverifiable rule (default-deny), ESCALATE, SAFE_HOLD or DO_NOTHING |

No level lets the agents send a person into a hazard without approval, and no level skips the critic.

## Data and licensing
All plant data is synthetic and labelled as such: the hall, people, incidents and SOPs are generated with seed 42 and
contain no real people or company data. They are shaped by real public data (UCI AI4I 2020, Bosch CNC vibration,
Open-Meteo weather, OSHA Severe Injury Reports, BLS injury rates, eCFR clause text, MovingAI maps), summarised in
[`data/seed/`](data/seed). Code and synthetic data are Apache-2.0; each public source keeps its licence (CC BY 4.0,
ODC-By or U.S. public domain), listed with its attribution in [DATA_LICENSE.md](DATA_LICENSE.md) and
[data/README.md](data/README.md). Details: [docs/data.md](docs/data.md).

## In depth
The README stays short; the details live in `docs/`.

| Topic | What it covers |
|---|---|
| [How an incident is handled](docs/agents.md#how-an-incident-is-handled-a-dynamic-orchestrator) | the dynamic orchestrator, every agent and the legal moves code allows |
| [Ask the floor](docs/agents.md#ask-the-floor-investigating-what-happened) | intake, coordinator, guarded SQL agent, log agent, analyst and evidence critic |
| [Autopilot](docs/agents.md#autopilot-a-whole-shift-many-incidents-at-once) | Sentinel detection, concurrent dispatch, leases on people and AGVs |
| [Models](docs/models-and-engines.md#models-which-model-serves-which-agent) | which Nemotron model serves which role, breakers, budgets, model pinning |
| [Meterless engines](docs/models-and-engines.md#meterless-engine-world-model) | World Model, H-MEM and Markovian: how each is ported, used and tested |
| [The plant and its data](docs/data.md#the-plant-fictional) | the fictional hall, what is real and synthetic, and the planted patterns |
| [Retrieval and failure handling](docs/reliability.md#retrieval-context-limits-and-failure-handling) | retrieval without a vector database, context limits, every retrieval failure |
| [Resilience](docs/reliability.md#resilience-what-happens-when-things-go-wrong) | what happens when models, Nightshift, Zetaris or the process fail |
| [What broke](docs/reliability.md#what-broke-and-how-we-fixed-it) | real failures from the build and how each was fixed |
| [Evaluation and chaos tests](docs/eval.md) | planted unsafe plans, Monitor calibration, 15 failure scenarios |
| [Production review](docs/production-readiness.md) | what a real deployment would still need |

## Limitations
- **Simulated floor.** The plant is a 2D grid simulation (one 2 m cell per 2 s tick), not a digital twin, and
  the connectors to fleet manager, MES and PLC are simulator implementations of real interfaces
  ([`app/connectors.py`](app/connectors.py)). Nothing drives real equipment. AGV dispatches are also emitted as VDA 5050 v2.0 orders
  and states ([`app/vda5050.py`](app/vda5050.py), `logs/vda5050.jsonl`), the standard real AGV fleets speak, but no MQTT broker or
  real vehicle is connected.
- **Synthetic data, real anchors.** Plant, people, incidents and the signal stream are generated, anchored on real
  public data (AI4I, Bosch CNC vibration, OSHA, BLS, Open-Meteo). Patterns were planted for the agents to find;
  costs are our assumptions. "Cars saved" is an estimate with a bootstrap 95% interval, not a measurement.
- **Rules are a sample, not a safety case.** About a dozen rules are checked; only the OSHA clauses are real (the
  SOPs are synthetic); a rule the critic cannot check blocks automatic action instead of being ignored. Clause
  excerpts are not legal advice.
- **Three models answer.** Nemotron 3 Ultra 550B, 3.5 Super VL and 3.5 Lightning answer our app;
  Llama-3.3-Nemotron-Super 49B is a judge lane that refuses direct requests. Ultra was pinned on the last build day,
  so the evaluation numbers above were measured with Super VL as the planner. Per-role pinning relies on lane
  allowlists in Nightshift's app policy (no cockpit control); the self-check and drift warnings make any change
  visible.
- **Nemotron's plan seldom replaces the rules plan.** The first plan (v1) is an optimal assignment for the same
  score the refine step compares, so Nemotron's v2 can tie it but rarely beat it on that score. Live, v2 mostly
  picked the same team, and when it differed the critic caught a rule it broke. Nemotron's value is in routing,
  re-planning after the critic, triage, investigation and the Lab; *Nemotron at work* shows the split.
- **Latency.** A live incident takes about 30-90 s and an investigation 1-3 min, mostly model time; the reflex is
  instant and the rules plan is ready in milliseconds. Free endpoints throttle under load, so live demos run two
  model calls per model at a time.
- **One process.** Incidents run concurrently inside one server; the journal makes a crash recoverable but there is
  no second node. The UI has no login: approvals carry a name, not an identity. Local demo only.
- **The Lab measures in the simulator.** An applied Lab change (a spill kit or a waiting AGV) is measured on later
  simulated incidents of that kind, with the same A* walk as the replays; it is a check of the prediction inside
  the model of the floor, not a field trial.
- **Detectors.** The Sentinel uses statistics (EWMA, CUSUM, trends) and rules, plus Lightning triage of ambiguous
  signals, not a learned anomaly model; the Bosch vibration features separate faults from noise only weakly.

## Disclosure
1. **Models (via Meterless Nightshift):**
   - `nvidia/nemotron-3-ultra-550b-a55b` first for the planner, analyst and Lab;
   - `nvidia/nemotron-3.5-super-vl-120b-a12b` (`NEMOTRON_DEEP`) for the coordinator, SQL agent, handover and zone
     briefing, and as the second choice for the planner, analyst and Lab;
   - `nvidia/nemotron-3.5-lightning-30b-a3b` for the router, monitor, Sentinel triage and evidence critic, and as
     the last fallback everywhere.
   The full map is under *Models*; `logs/llm.jsonl` and the receipts show which model answered each call.
2. **Agent frameworks:** none. The orchestration is our own code ([`app/dynamic.py`](app/dynamic.py), [`app/orchestrator.py`](app/orchestrator.py)), using Pydantic contracts
   for strict JSON between agents.
3. **Integrations:** Zetaris, Meterless (World Model, H-MEM, Markovian, Nightshift) and NVIDIA, as described under Sponsor technology.
4. **AI coding tools:** Claude Code (Anthropic) wrote most of the code and documentation; Cursor wrote the model
   layer ([`app/llm.py`](app/llm.py), [`tests/test_llm.py`](tests/test_llm.py)). The team reviewed and tested all of it.
5. **External libraries:** openai, python-dotenv, requests, pydantic and pytest; h5py and numpy only for the
   optional data rebuild.
6. **Data:** the plant, people, incidents and SOPs are synthetic, anchored on public data (see "What is real and
   what is synthetic").
7. **Adapted open source:**
   - [`app/engines/world_model.py`](app/engines/world_model.py) and [`app/engines/hmem.py`](app/engines/hmem.py) are Python ports of the Meterless engine specs and
     reference code (github.com/Meterless/Meterless, Apache-2.0).
   - The H-MEM conformance vectors are copied unchanged.
   - The MovingAI maps are included under ODC-By.
8. **Known limitations:** see below.
9. **Fallback behaviour:**
   - `SAMPLE_MODE=true`: recorded replies plus a rule-based planner, the offline plant and no network.
   - Circuit-breaker safe mode when Nightshift fails: a rule-based plan and ESCALATE.

## Pre-existing components
None. The repository was started inside the build window (first commit on 23 October 2026). Open-source
components used: the Meterless engine specs (Apache-2.0), the Zetaris getting-started kit (used as
documentation only), and the public datasets listed above.

## Team
**Team Spartans**

| Name | GitHub |
|---|---|
| Hemanth Raja Guttikonda | [@Hemanth-Raja](https://github.com/Hemanth-Raja) |
| Poojitha Katta | [@Poojitha-Katta](https://github.com/Poojitha-Katta) |

## License
- **Code:** Apache-2.0, see [LICENSE](LICENSE). The Meterless engine ports follow the Apache-2.0 Meterless specs.
- **Data:** see [DATA_LICENSE.md](DATA_LICENSE.md). The synthetic plant data is ours and Apache-2.0; the public
  sources in [`data/seed/`](data/seed) keep their own licences (CC BY 4.0, ODC-By or U.S. public domain), with the
  attribution each one requires.
