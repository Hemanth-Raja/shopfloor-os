# Shop-Floor OS

**AI agents plan, robots execute, humans judge.** A multi-agent incident-response and AGV-dispatch system for a
car final-assembly hall. Open Agent Hackathon 2026, **Track 3: Reasoning Architecture**.

**The problem.** On an assembly line, a missing kit, a stalled AGV, a fluid spill or an overheating machine
stops cars, and it can also put people at risk. Shift supervisors have to decide in seconds:
- who to send and which robot to re-route;
- whether an alarm is real or sensor noise;
- whether acting is safe under OSHA rules;
- whether to stop the line at all.

**Who it is for:** shift supervisors, team leaders and maintenance planners in vehicle assembly.

**What it does.**
- Agents read the plant's fragmented data (fleet, people, plant, operations) through Zetaris in one federated
  query and build a grounded **Meterless World Model**.
- They plan with **Nemotron** via Meterless Nightshift.
- A deterministic safety critic checks every plan, citing the exact OSHA clause.
- Each plan is simulated before it is executed and verified afterwards.
- A restraint gate decides whether to ACT, VERIFY, hold the area safe or ESCALATE to a human, who approves or
  rejects in the web UI.
- Lessons from every incident go into **Meterless H-MEM**, so the same mistake is not made twice.

## The plant (fictional)
A car final-assembly hall on a 20 x 40 grid (one cell is about 2 m):
- **10 stations:** body weld cell, hemming press, paint booth, two trim stations, powertrain marriage, three final-assembly stations and end-of-line test.
- **Areas:** a parts supermarket and kitting area, the receiving dock, a rework bay, AGV charging, and pedestrian walkways that cross the AGV lanes.
- **Fleet:** 8 AGVs: 5 kitting tuggers and 3 heavy body carriers. AGV-3 is old and unreliable.
- **People:** 24 fictional workers on shifts A (06-14), B (14-22) and N (22-06), with lockout/tagout, forklift and robot-cell certifications and with absences.
- **Production:** one car every 75 s, with 8 machines.
- **History:** 90 days, 20 Jul to 17 Oct 2026, with about 2,000 incidents. Each incident has time to clear, downtime, cars lost and an estimated cost.

| Zetaris datasource | Tables |
|---|---|
| sf_fleet | agvs, agv_tasks, agv_telemetry (every minute, last 7 days), fleet_hourly |
| sf_hr | workers, certifications, shifts |
| sf_plant | stations, machines, machine_telemetry (hourly, 90 days), vibration_samples, weather_hourly, production_plan, units, work_orders |
| sf_ops | zones, floor_cells, spill_kits, safety_rules, incidents, andon_events, data_sources, experiments |

## Setup
### 1. From a clean clone, without Zetaris, Nightshift or keys (sample mode)
```
git clone <this repo> && cd shopfloor-os
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env                       # an empty .env is fine for sample mode
.venv/bin/python run.py run input_examples/example_1.json
SAMPLE_MODE=true .venv/bin/python -m app.server       # web UI on http://127.0.0.1:8000
.venv/bin/python -m pytest -q                          # 115 offline tests
```
With Docker: `docker build -t shopfloor-os .` then `docker run --rm -p 8000:8000 shopfloor-os`. The image is
tested (174 MB) and defaults to `SAMPLE_MODE=true`.

`SAMPLE_MODE=true` still runs the real agent loop on the offline plant: simulator, A*, critic, dry-run,
restraint gate, executor and verifier. Recorded Nemotron replies (`logs/fixtures/`) are used where one exists
for that exact input. Otherwise a deterministic rule-based planner stands in, and it is still checked by the
critic. No output is read from `output_examples/`.

### 2. Live, with Zetaris and Meterless Nightshift
1. Install and start Zetaris (Docker) and Meterless Nightshift as described in the Participant Setup Guide and
   [zetaris.github.io/getting-started](https://zetaris.github.io/getting-started/).
2. Put the Zetaris REST URL, org id and API key, and the Nightshift base URL and runtime token, into `.env`
   (see `.env.example`). Choose a Postgres password for `PG_PASSWORD`. The NVIDIA key stays in Nightshift.
3. Load the data and register it in Zetaris (fully scripted, no manual steps):
```
bash scripts/start_postgres.sh          # Postgres container on the Zetaris Docker network
bash scripts/load_data.sh               # generate the plant from data/seed and load fleet, hr, plant, ops
.venv/bin/python -m app.zetaris register     # first time: create the sf_* Zetaris datasources
.venv/bin/python -m app.zetaris refresh      # after any reload
.venv/bin/python run.py health               # Zetaris and Nightshift through their circuit breakers
```
In Docker, use `host.docker.internal` instead of `localhost` for Zetaris and Nightshift.

Or do steps 3 onwards in one go: `bash scripts/start_live.sh` (idempotent; it starts the UI on a free port). The live demo runbook is `docs/live-demo.md`.

## Usage
| Entry point | Command |
|---|---|
| **JSON in, JSON out** (programmatic) | `.venv/bin/python run.py run input_examples/example_2.json` |
| **HTTP API** | `POST /api/run` with the same JSON body (`python -m app.server`) |
| **Web UI** | `python -m app.server`: live floor, Lab and "What the agents know" tabs, Approve/Reject |
| One incident (CLI trace) | `run.py incident --kind agv_fault --agv AGV-3 --station F30 --stall-after 2` |
| Zone briefing | `run.py context --zone FINAL` (World Model built from one federated Zetaris query, plus a Nemotron summary) |
| **Ask the floor** (investigation) | `run.py ask "Why does F10 keep stopping?"` or the *Ask the floor* tab; `--json` prints the full result |
| Custom incident | the *Report your own* form under the map (kind, station, severity, note), or `input.severity` / `input.note` in the JSON contract |
| Lab experiment | `run.py lab [--write]` (hypothesis, then 20 seeded replays, then bootstrap CI, then an `sf_ops.experiments` proposal) |

Request:
```json
{"run_id": "example-2",
 "input": {"kind": "agv_fault", "agv": "AGV-3", "station": "F30", "stall_after": 2, "seed": 42},
 "options": {"sample_mode": false, "memory": false}}
```
- `input.kind` is one of `part_shortage`, `agv_fault`, `overheat`, `spill`, `robot_cell_fault`, `torque_fault`,
  `near_miss` or `quality_defect`. A kind without a response playbook is fenced and escalated to a supervisor, with the
  reason, instead of being planned.
- Optional `severity` (`low`, `medium`, `high`, `critical`) and `note` (free text from the reporter).
- `station` is a station id (B10, B20, P10, T10, T20, M10, F10, F20, F30, E10).
- `false_alarm` makes an overheat sensor noise.
- `stall_after` stalls the dispatched AGV mid-route.

The response contains:
- `status`;
- `output`: decision, score, reasons, plan, citations, critic_rejections, replans, safe_mode, collisions;
- `agents`, with their roles;
- `models`, naming which model handled each request;
- `trace_id`, `log_file` and `execution_time_seconds`.

Failures return `status: "error"` with a typed `error` (`InvalidInput`, `UpstreamUnavailable`, and so on) rather
than a crash.

### Sample inputs and outputs
| Input | What happens | Output |
|---|---|---|
| `input_examples/example_1.json`: part shortage at F30 | The planner sends AGV-5 at 1.0 m/s through the X-WEST crossing. The critic rejects it under 29 CFR 1910.178(n)(4); the re-plan slows to 0.5 m/s. Result: ACT | `output_examples/example_1.json` |
| `example_2.json`: AGV-3 fault plus a stall | The replacement AGV-1 stalls mid-route. The verifier detects it and the loop re-plans with AGV-2. Result: ACT | `example_2.json` |
| `example_3.json`: overheat at F10, false alarm | The temperature is over its threshold, but the Bosch vibration recording is normal, so the sensors disagree. Result: VERIFY (a technician checks; the line keeps running) | `example_3.json` |
| `example_4.json`: coolant spill at F20 | A two-cell geofence goes up and AGVs stay out. The spill responder goes in, citing 29 CFR 1910.22. Result: ACT | `example_4.json` |

### Logs
**Watch it live:**
- **Flow tab** in the web UI: a box per agent. The active box lights up, retries and re-plans count up, and clicking a box shows what it received and produced, including what was sent to Nemotron, the model that answered and the latency.
- **`python run.py watch`** in a second terminal: one coloured, plain-English line per agent step, Nemotron call and human judgement, for example `12:03:04 PLANNER  1.2s  Nemotron-Super attempt 2 -> send W-18; AGV-5->F30 1.0/0.5 m/s`.

Every run writes `logs/trace-<run_id>.jsonl`, one JSON object per agent step. Each record has the agent, ok,
timing, inputs and outputs, critic violations with clauses, the model that answered, and retries and re-plans.
Every step is also printed as an `[INFO] run_id=… agent=…` line on stdout.

Other logs:
- `logs/llm.jsonl` records every Nightshift request with the requested and answering model and the latency.
- `logs/world_model.jsonl` records World Model builds.
- `logs/approvals.jsonl` records human judgements.

None of them contain secrets.

## Agents
| Agent | Role | Model |
|---|---|---|
| reflex | Geofences the incident and stops or re-routes AGVs, in under 100 ms | none (rules) |
| monitor | Real alarm or sensor noise, from thresholds plus Bosch vibration evidence; one-line summary | NEMOTRON_FAST (summary only) |
| memory | Recalls H-MEM lessons from past incidents before planning | none (H-MEM ranking) |
| candidates | Top-3 qualified workers and healthy AGVs from the World Model (valid certifications; no service-overdue AGV for critical work) | none |
| planner | Chooses among the candidates; strict JSON; must cite the governing clauses | NEMOTRON_DEEP |
| safety critic | Deterministic OSHA-based rule engine; cites the exact clause per violation; the planner gets up to 3 tries | none |
| dry-run | 60 s headless simulation with A* and a reservation table; fails on a collision or a late ETA | none |
| restraint | ACT, VERIFY, SAFE_HOLD, ESCALATE or DO_NOTHING, from confidence, sensor agreement and cost | none |
| executor / verifier | Dispatches in the sim and checks the world changed as predicted; re-plans on mismatch | none |
| health | Circuit breakers for Zetaris and Nightshift; safe mode when Nightshift is down | none |
| lab | Policy hypothesis, then 20 seeded replays (no LLM), then a bootstrap 95% CI | NEMOTRON_DEEP (hypothesis only) |

## Ask the floor: investigating what happened
The incident loop reacts to one incident. *Ask the floor* looks back over everything that happened. A question such
as "Why does F10 keep stopping?" goes to a team of agents (`app/investigate/`):

| Agent | Role | Model |
|---|---|---|
| coordinator | Splits the question into 1–4 sub-tasks: SQL over the plant data, and the agents' own logs | NEMOTRON_DEEP |
| SQL agent | Writes one read-only Spark SQL query per sub-task from the table catalogue, runs it on Zetaris; on a guard refusal or Zetaris error it gets the message and one retry. A task that builds on another runs after it and sees its rows | NEMOTRON_FAST |
| SQL guard | One `SELECT`/`WITH` only, no `;`, no DDL/DML/`SET`/`REFRESH`, only whitelisted `sf_*` tables, `LIMIT` capped at 200 | none (rules) |
| log agent | Searches `logs/trace-*.jsonl` (decisions, critic rejections by rule, re-plans), `logs/llm.jsonl` (calls, latency, models) and H-MEM lessons | none |
| analyst | Correlates the evidence into findings, each citing evidence ids (E1, E2…) | NEMOTRON_DEEP |
| critic | Drops any finding that cites missing or failed evidence, or quotes a number not found in the cited rows (totals of up to three values in one column are allowed) | none (rules) |

Independent sub-tasks run in parallel. The answer shows its evidence: the SQL, row counts, the top rows and log
references, plus the claims the critic dropped and why. Each step streams to the UI and to `run.py watch`, and is
written to `logs/trace-ask-<id>.jsonl`. Without Nightshift, a rule-based coordinator and analyst use fixed queries.
In sample mode the three example questions replay recorded Nemotron replies and Zetaris rows; any other question
runs on an offline SQLite copy of the four sources, built from `data/seed` on first use.

## How an incident is handled
```
reflex (geofence, stop/reroute AGVs, < 100 ms, rules only)
 -> monitor (thresholds + Bosch vibration evidence; NEMOTRON_FAST one-line summary)
 -> memory (H-MEM lessons from past outcomes)
 -> candidates (one federated Zetaris query -> Meterless World Model: top workers and AGVs)
 -> planner (NEMOTRON_DEEP, strict JSON, grounded in the world model, cites clauses) <-> safety critic (OSHA rules + SOPs, up to 3 tries)
 -> dry-run (60 s headless sim, A* with a reservation table) -> restraint (ACT | VERIFY | SAFE_HOLD | ESCALATE | DO_NOTHING)
 -> executor -> verifier (re-plans when the world did not change as predicted) -> human approve/reject (web UI)
```
**Autonomy and the human gate.** The UI switch, or `options.autonomy` in the API, sets how much the agents may do alone:
- **Auto (default):** a routine ACT that passed the critic and the dry-run runs at once, and a person can *Roll back*, which stops the agents and sends them back. A person entering a hazard (spill clean-up, machine repair), a lapsed certificate, or an ESCALATE, SAFE_HOLD or DO_NOTHING decision waits for *Approve*.
- **Supervised:** every dispatch waits for approval.
- **Advisory:** the agents only recommend, and a person dispatches.

On the map, the agents dispatched for the current incident have a yellow ring; everything else is normal shift activity. Every judgement is logged to `logs/approvals.jsonl`.

Every loop is bounded:
- critic retries: 3;
- re-plans: 3;
- Zetaris requests: 60 s;
- planner calls: 90 s.

Every external dependency sits behind an adapter: `app/zetaris.py` and `app/llm.py`, wrapped by the circuit
breakers in `app/runtime.py`.

## Dependencies
- **Python 3.11+** and `requirements.txt`: openai (the OpenAI-compatible client for Nightshift), python-dotenv,
  requests, pydantic and pytest. The generator, simulator, engines and web server use only the standard library.
- **Live mode:** Zetaris (local Docker), Postgres 16 (Docker container `shopfloor-pg`, created by
  `scripts/start_postgres.sh`) and Meterless Nightshift, with NVIDIA's hosted Nemotron models. No GPU is needed.
- **Optional rebuild of `data/seed/`:** `requirements-data.txt` (h5py), used only by `app/data/fetch_sources.py`.

## Sponsor technology
- **Zetaris (the data layer).** Four Postgres sources are registered as Zetaris datasources: `sf_fleet`,
  `sf_hr`, `sf_plant` and `sf_ops`.
  - Every incident starts with **one federated Lightning SQL query** across all four (`app/engines/plant_world.py`).
  - The zone briefing and the lab history queries also run through Zetaris.
  - Lab proposals are written back through Zetaris (`INSERT INTO sf_ops.experiments`).
- **Meterless.** Two engines are implemented and used in features, each with its conformance tests:
  - The **World Model** (`app/engines/world_model.py`) grounds every plan. `tests/test_world_model.py` ports
    the spec's 8 conformance checks.
  - **H-MEM** (`app/engines/hmem.py`) holds the incident playbooks and the OSHA/SOP clause index.
    `tests/test_hmem.py` runs all 13 official vectors.
  - **Meterless Nightshift** is the only model provider: every call goes through `app/llm.py` to
    `MODEL_BASE_URL`.
- **NVIDIA.** Nemotron models reached through Nightshift:
  - `nvidia/nemotron-3.5-super-vl-120b-a12b` (deep lane) plans, re-plans after critic feedback and writes lab
    hypotheses.
  - `nvidia/nemotron-3.5-lightning-30b-a3b` (fast lane) writes the monitor's one-line summaries.
  - `logs/llm.jsonl` and the trace record which model actually answered each request.
- **Cursor.** The project rules live in `.cursor/rules/shopfloor.mdc` and `AGENTS.md`. Cursor built the model
  layer (`app/llm.py`) and its tests. Claude Code was also used (see Disclosure).
- **What is unique:**
  - Two Meterless engines work together. The World Model grounds ids and facts; H-MEM ranks both lessons and
    regulatory clauses.
  - The LLM never acts unchecked: every plan is critic-checked with clause citations, simulated, gated by
    restraint and verified.

## Meterless engine: World Model
Shop-Floor OS implements the **Meterless World Model** engine in Python, in `app/engines/world_model.py`. It is a
port of the spec in github.com/Meterless/Meterless `engines/world-model` (Apache-2.0), and a reference copy of the
spec and conformance runner can be fetched into `data/raw/meterless/`.
- **The engine:** an event-sourced store of entities, contexts and bitemporal relationships, with stable
  content-derived ids, provenance on every write, banded name resolution (auto-merge at 0.85 or above, review queue
  from 0.82), idempotent ingest, an ordered audit log, and graph, facts, snapshot and timeline views.
- **The feature:** `run.py context --zone FINAL` sends one federated Zetaris query, and
  `app/engines/shopfloor_world.py` builds the zone's world from it:
  - incidents, stations, machines, workers, AGVs and Bosch vibration recordings, with their links;
  - grounded checks, such as whether the responder's lockout/tagout was valid and whether the AGV's service is
    overdue.

  The Nemotron prompt receives the world model's structured context payload, not raw rows. `ground()` lets a
  planner reject entity ids that are not in the world.
- **Tests:** `tests/test_world_model.py` ports the spec's conformance checks (sid-001, sid-002, band-001,
  prov-001, idem-001, idem-002, bitemp-001 and audit-001) plus the reference smoke tests.
  `tests/test_shopfloor_world.py` covers the shop-floor builder. Run them with `.venv/bin/python -m pytest`.

## Meterless engine: H-MEM
The incident playbooks (`app/engines/playbooks.py`) run on a Python port of the **Meterless H-MEM** engine,
in `app/engines/hmem.py`. It follows the spec in github.com/Meterless/Meterless `engines/hmem` (Apache-2.0).
- Each critic lesson is stored as an H-MEM record with its source incident, tags and a confidence score.
- Before planning, the orchestrator recalls lessons with H-MEM's 8-signal hybrid ranking. Every outcome then
  feeds back into the record: helpful when the lesson held, not_helpful when the rule was broken again anyway.
  All of it goes through the append-only trust ledger.
- Sleep, conflict resolution and dreaming are implemented with the spec's guardrails.
- `tests/test_hmem.py` runs all 13 official conformance vectors, copied unchanged into `tests/fixtures/`.

## What is real and what is synthetic
**Synthetic.** These are generated by `app/data/generate.py` with seed 42, so every run gives the same data:
- the layout, stations, machines, AGVs and their tasks and telemetry;
- the people, certifications and rosters;
- the plant SOPs in `data/seed/plant_sops.json`, written for this fictional plant;
- production, units, incidents, andon pulls and work orders;
- the cost figures. These are our own assumptions for ranking incidents, not industry figures: $1,500 per minute of line stop, $85 per labour hour and $400 per rework.

No open dataset of car-plant operations (AGVs, andon, downtime) was found under an open licence, so these parts are simulated. Each one is shaped by the real data below.

**Real data used.** The summaries are stored in `data/seed/` and committed. If a file is missing, the generator uses a documented fallback, and the table `sf_ops.data_sources` records which source was used.

| Source | Used for | Licence and credit |
|---|---|---|
| UCI AI4I 2020 Predictive Maintenance | Hourly machine telemetry (air and process temperature, rpm, torque, tool wear, failure flags). Rows are reused across machines, and temperatures are shifted with the outdoor weather. | CC BY 4.0. Matzka, S. (2020), UCI Machine Learning Repository, DOI 10.24432/C5HS5C. AI4I itself is a synthetic dataset modelled on real machines. |
| OSHA Severe Injury Reports, NAICS 3361 (Jan 2015 to Nov 2025, 321 reports) | The mix of safety-event types (vehicle/pedestrian, caught in machinery, slips, heat and so on). Only aggregated counts by event type are stored; employer names, addresses, locations and narratives are dropped on download. | Public domain, U.S. Department of Labor. No endorsement implied. |
| BLS Survey of Occupational Injuries and Illnesses, NAICS 3361 | 5.6 recordable cases per 100 full-time workers (2024). That rate × 24 workers × 90 days gives the expected recordable injuries, and Heinrich's 1:29:300 ratio (a heuristic) scales it to about 110 safety events. | Public domain, U.S. Bureau of Labor Statistics. |
| Open-Meteo historical weather, Detroit MI | Hourly outdoor temperature. It heats the machines and drives overheat alarms. | CC BY 4.0. Weather data by Open-Meteo.com |
| Bosch CNC Machining dataset (UCI 752), 3-axis vibration at 2 kHz from three CNC machines | `vibration_samples`: all 70 recordings labelled abnormal plus a stratified sample of 280 normal ones, summarised per axis (RMS, peak, crest factor, kurtosis). The raw HDF5 files are not kept. Each torque alarm on the wheel nutrunner M-06 cites a recording: an abnormal one for real faults, a normal one for false alarms, so agents can check fault against noise. These are milling-machine signals used as a stand-in for nutrunner vibration. | CC BY 4.0. Tnani, M.-A., Feil, M., Diepold, K. (2022), "Smart Data Collection System for Brownfield CNC Milling Machines: A New Benchmark Dataset for Data-Driven Machine Monitoring", Procedia CIRP 107. github.com/boschresearch/CNC_Machining |
| MovingAI MAPF benchmarks: 4 warehouse maps | `data/seed/mapf/*.map`, kept for path-planner benchmarks. The 200 matching scenario files are an optional download into `data/raw/mapf/`. | Open Data Commons Attribution License. Stern, R. et al. (2019), "Multi-Agent Pathfinding: Definitions, Variants, and Benchmarks", SoCS. movingai.com/benchmarks/mapf |
| Meterless H-MEM engine spec and conformance vectors | `app/engines/hmem.py` and `tests/fixtures/hmem_vectors.json` (unchanged copy) | Apache-2.0. github.com/Meterless/Meterless, engines/hmem |
| Meterless World Model engine spec | `app/engines/world_model.py`, a Python implementation of the spec, with the conformance checks ported to pytest | Apache-2.0. github.com/Meterless/Meterless, engines/world-model |
| OSHA 29 CFR 1910.22, 1910.147, 1910.178, 1910.212, 1910.217 | Paraphrased rule summaries in `safety_rules`, plus 14 verbatim clause excerpts (`data/seed/osha_1910_excerpts.json`, eCFR) that the critic and planner cite. These are indexed in H-MEM together with 7 synthetic plant SOPs (`data/seed/plant_sops.json`). | Public domain, U.S. Department of Labor, via eCFR (ecfr.gov). No endorsement implied. |

## Patterns in the data (planted, for the agents to find)
- F10 Wheels and tyres is the hot spot for part shortages, torque faults and defects.
- AGV faults concentrate on AGV-3, which is old, low on battery health and overdue for service.
- Shortages rise when the kitting fleet is busy, either from high demand or an AGV being down.
- Time to clear grows with distance from the supermarket (shortages) and from the nearest spill kit (spills).
- Machine overheats are about three times as likely in hot hours, and about a quarter are false alarms from noisy sensors.
- Some wheel-nutrunner alarms are sensor noise (20 of 150 in this build); the linked Bosch vibration recording shows which.
- The night shift is thinly staffed and clears incidents more slowly.
- Some repairs were done by people whose lockout/tagout or forklift certification had lapsed.
- Body carriers cross the west pedestrian crossing faster than the 0.5 m/s rule allows.

All people, car models and the plant itself are fictional.

## Disclosure
1. **Models (via Meterless Nightshift):**
   - `nvidia/nemotron-3.5-super-vl-120b-a12b` (NEMOTRON_DEEP) for planning and lab hypotheses;
   - `nvidia/nemotron-3.5-lightning-30b-a3b` (NEMOTRON_FAST) for monitor summaries;
   - `meterless/auto` in the zone briefing. Nightshift may route it to another model; the logs show which one answered.
2. **Agent frameworks:** none. The orchestration is our own code (`app/orchestrator.py`), using Pydantic contracts
   for strict JSON between agents.
3. **Integrations:** Zetaris, Meterless (World Model, H-MEM, Nightshift) and NVIDIA, as described under Sponsor technology.
4. **AI coding tools:** Claude Code (Anthropic) wrote most of the code and documentation; Cursor wrote the model
   layer (`app/llm.py`, `tests/test_llm.py`). The team reviewed and tested all of it.
5. **External libraries:** openai, python-dotenv, requests, pydantic and pytest; h5py and numpy only for the
   optional data rebuild.
6. **Data:** the plant, people, incidents and SOPs are synthetic, anchored on public data (see "What is real and
   what is synthetic").
7. **Adapted open source:**
   - `app/engines/world_model.py` and `app/engines/hmem.py` are Python ports of the Meterless engine specs and
     reference code (github.com/Meterless/Meterless, Apache-2.0).
   - The H-MEM conformance vectors are copied unchanged.
   - The MovingAI maps are included under ODC-By.
8. **Known limitations:** see below.
9. **Fallback behaviour:**
   - `SAMPLE_MODE=true`: recorded replies plus a rule-based planner, the offline plant and no network.
   - Circuit-breaker safe mode when Nightshift fails: a rule-based plan and ESCALATE.

## Limitations
- The floor is a 2D grid simulation, not a digital twin: AGVs move one 2 m cell per 2 s tick, and kinematics are
  simplified.
- The plant, people and incidents are synthetic. Patterns were planted for the agents to find, and the cost
  figures are our own assumptions.
- The Bosch vibration features separate real faults from noise only weakly (they come from milling machines,
  standing in for a nutrunner). The monitor treats them as one piece of evidence, not a classifier.
- One incident is handled at a time. There is no arbitration between simultaneous incidents competing for the
  same AGVs and people.
- Deep-lane planning takes about 5–90 s per call, depending on Nightshift routing and load.
- Paraphrased rule summaries and short clause excerpts are not legal advice. Check the full eCFR text.
- Not production-ready: there is no authentication on the web UI, and it is intended for a local demo.

## Pre-existing components
None. The repository was started inside the build window (first commit on 23 October 2026). Open-source
components used: the Meterless engine specs (Apache-2.0), the Zetaris getting-started kit (used as
documentation only), and the public datasets listed above.
