# Agents in depth

Part of the [Shop-Floor OS README](../README.md); the README has the summary of each section.

## Ask the floor: investigating what happened
The incident loop reacts to one incident. *Ask the floor* looks back over everything that happened. A question such
as "Why does F10 keep stopping?" goes to a team of agents ([`app/investigate/`](../app/investigate)):

| Agent | Role | Model |
|---|---|---|
| intake | Works out what the message is before anything goes to SQL. Code settles the clear cases, so they work with Nemotron down: a greeting gets a briefing of the live floor (by the real clock and shift); "someone is hurt" gets people-first instructions and never a model; a write ("delete all incidents") is refused; an action ("send AGV-3 to F30") is handed to the Live floor; a report ("coolant spill at F20") is checked against open incidents and pre-fills the report form; a follow-up ("and F20?") is rewritten against the last question. Only unclear messages go to the coordinator model, and its answer is validated | rules, then `coordinator` role |
| coordinator | Splits the question into 1–4 sub-tasks: SQL over the plant data, and the agents' own logs | `coordinator` role |
| SQL agent | Writes one read-only Spark SQL query per sub-task from the table catalogue, runs it on Zetaris; on a guard refusal or Zetaris error it gets the message and one retry. A task that builds on another runs after it and sees its rows | `sql_agent` role |
| SQL guard | One `SELECT`/`WITH` only, no `;`, no DDL/DML/`SET`/`REFRESH`, only whitelisted `sf_*` tables, `LIMIT` capped at 200 | none (rules) |
| log agent | Searches `logs/trace-*.jsonl` (decisions, critic rejections by rule, re-plans), `logs/llm.jsonl` (calls, latency, models) and H-MEM lessons | none |
| analyst | Correlates the evidence into findings, each citing evidence ids (E1, E2…) | `analyst` role |
| critic | First rules: drops any finding that cites missing or failed evidence, or quotes a number not found in the cited rows (totals of up to three values in one column are allowed). Then a different model re-checks the remaining claims against the rows; it can only drop | rules, then `evidence_critic` role |

Independent sub-tasks run in parallel. The answer shows its evidence: the SQL, row counts, the top rows and log
references, plus the claims the critic dropped and why. Each step streams to the UI and to `run.py watch`, and is
written to `logs/trace-ask-<id>.jsonl`. Without Nightshift, a rule-based coordinator and analyst use fixed queries.
In sample mode the three example questions replay recorded Nemotron replies and Zetaris rows; any other question
runs on an offline SQLite copy of the four sources, built from [`data/seed`](../data/seed) on first use.

**Asked earlier.** Every answer is kept with its time ([`app/askhistory.py`](../app/askhistory.py), `GET /api/asks`). Asking the same
question again shows what changed since: the old summary, findings that are new or gone, and the evidence row
counts then and now, because new incidents arrive and the model may write a different query.

### The agents' own history, in Zetaris
The JSONL files in [`logs/`](../logs) are the source of truth (they work offline and survive a Zetaris outage). Every 30 s the
live server also copies new lines into Postgres ([`app/logsync.py`](../app/logsync.py), or `run.py logsync`), and Zetaris federates them
next to the plant data: `sf_ops.agent_steps` (one row per agent step), `sf_ops.llm_calls` (requested vs answering
model, latency, tokens) and `sf_ops.journal` (the hash-chained shift journal). Ask the floor sees these tables only
for questions about the agents, for example:

> *Which safety rule does the critic reject most often, and how often did the planner model answer as requested?*
> R-TORQUE-QA (233 rejections), then R-CROSSING (201); the planner was served the requested model in 77 of 78 calls.

## Autopilot: a whole shift, many incidents at once
`run.py shift --minutes 8 --seed 7 --rate 2.5` runs headless; the **Run shift** form under the map runs it in the UI.

```
floor signals --> Sentinel (rules; dedupe; correlate; Lightning triage of ambiguous signals only)
             --> priority queue (people at risk, then severity, then expected cost)
             --> dispatcher: N incidents at once, each on its own dynamic route
             --> leases on people and AGVs (pre-emption by risk; expiry) --> human gate per autonomy level
```
- **Sentinel** ([`app/autonomy/sentinel.py`](../app/autonomy/sentinel.py)) watches a stream of floor signals. The stream is generated from the
  historical incident mix in `sf_ops.incidents`, deterministic for a seed. The signals are line-side stock, AGV
  status, machine temperature with Bosch vibration, spill cameras, robot controllers, nutrunner torque, proximity
  and quality checks, plus continuous telemetry (machine temperature every 30 s, stock every 20 s).
  - Adaptive detection ([`app/autonomy/detectors.py`](../app/autonomy/detectors.py)): each machine's temperature baseline per shift comes from
    `sf_plant.machine_telemetry` and then follows the stream by EWMA (learning only from normal readings);
    |z| >= 3 is an anomaly and 2-3 goes to triage; a CUSUM raises an early warning on a slow drift before any
    single reading looks abnormal. Line-side stock is projected from its consumption trend, so a shortage is
    raised before the 5-minute floor. Hard limits are fixed per machine (historical mean + 4 sd, at least 313 K)
    and person-safety limits never adapt.
  - Rules raise the clear cases.
  - A repeat alarm is merged while its incident is open, and an AGV fault absorbs the shortage it causes at the
    station it was feeding.
  - Only ambiguous readings are triaged by the `sentinel` role (Lightning). A person-safety signal can never be
    dismissed.
  - A station failing repeatedly in one shift gets a higher severity; a persisting condition is not raised again
    right after its incident is handled unless it gets worse (a hard-limit breach always gets through).
  - Every decision is logged in `logs/sentinel.jsonl`.
- **Dispatcher** ([`app/autonomy/dispatcher.py`](../app/autonomy/dispatcher.py)) keeps a priority queue and runs several incidents at once, each
  with its own orchestrator.
  - An incident that puts people at risk jumps in even when every slot is busy.
  - The orchestrator is a step generator, so incidents interleave hop by hop. This is deterministic in sample
    mode; live, each incident gets a thread, so model calls overlap.
  - Hops that move things on the floor take the floor lock.
- **Leases** ([`app/autonomy/leases.py`](../app/autonomy/leases.py)) stop two incidents from sending the same person or robot.
  - Candidates skip resources held by an equally or more urgent incident, and the critic rejects a plan that uses
    one (`R-LEASE`).
  - A more urgent incident pre-empts a less urgent one, which re-plans. The trace shows this as an `arbiter` step.
  - Leases expire (5 minutes of floor time), so nothing deadlocks.
  - Responders walk back once their job is done, and each incident has its own geofence.
- **Human gate:** the autonomy setting still applies. The Shift panel lists what is waiting for you, with
  Approve and Reject per incident, and clicking an incident opens its route in the *Incidents* tab.
- **Summary:** incidents, decisions, how many were resolved without a person, how many waited, escalations, mean
  time to resolve, pre-emptions, peak concurrency, quality holds (MES), and cars saved with a bootstrap 95%
  interval over real historical incidents of each kind.
- **Durable:** the queue, leases (with fencing tokens), dispatches, approvals and results go to a hash-chained
  journal (`logs/journal.jsonl`, replicated to Postgres `ops.journal`); after a crash, interrupted incidents are
  handed to a person (`run.py audit` verifies the chain).
- **Runtime safety:** an invariant monitor checks the floor after every tick, independently of the critic.

**Caching** ([`app/cache.py`](../app/cache.py), hit rates in `/health`):
- read-only Zetaris results, 120 s, cleared by any write;
- incident history per kind, station and 10 minutes;
- H-MEM clause retrieval;
- counted replay hits in sample mode.

## How an incident is handled: a dynamic orchestrator
There is no fixed pipeline. After the reflex, an **orchestrator agent** ([`app/dynamic.py`](../app/dynamic.py)) chooses which agent runs
next, hop by hop. At each decision point the router model (role `router` in [`config/models.json`](../config/models.json)) gets a compact
state and the agents that are legal right now, and returns strict JSON `{next_agent, reason, args}`.

| Agent | What it does | Model |
|---|---|---|
| reflex | Geofence, stop or re-route AGVs, in under 100 ms. Always first | none (rules) |
| monitor | Real alarm or sensor noise, from thresholds and Bosch vibration evidence; one-line summary | `monitor` role |
| memory | H-MEM lessons and the playbook for this kind and station | none (H-MEM ranking) |
| investigator | History through Zetaris (guarded SQL) for this kind, station and AGV: false-alarm share, downtime | none |
| candidates | Top qualified people and healthy AGVs from the World Model (one federated Zetaris query) | none |
| playbook | Re-uses a proven H-MEM playbook on today's candidates: no model call | none |
| check first (`verify_plan`) | One qualified person inspects before anything else moves: no model call | none |
| planner (anytime) | v1 at once by optimal assignment (Hungarian algorithm over roles × candidates: travel, reliability, load; infeasible if a skill or certificate is missing); Nemotron's strict-JSON v2 in the background | none for v1, `planner` role for v2 |
| refine | Checks v2 with the critic and the dry run; swaps it in only if it is safe and scores better | none (rules) |
| safety critic | OSHA rules and SOPs with exact clause citations; violations go back as feedback | none (rules) |
| dry run | 60 s headless simulation, A* with a reservation table | none |
| restraint | ACT, VERIFY, SAFE_HOLD, ESCALATE or DO_NOTHING from confidence, sensors and cost | none |
| human gate, executor, verifier | Autonomy-level approval; dispatch; check the world changed as predicted | none |

**Guardrails are code, not prompts.** The model can only choose among the legal agents, and:
- the reflex always runs first;
- the executor runs only after the latest plan passed the critic and the dry run, and the human gate allowed it;
- the human gate always runs before anything moves;
- an illegal or unknown choice is rejected, logged as a `router` step, and the rule-based router decides;
- once Nightshift is unavailable, the planner is replaced by safe mode (rule-based plan, ESCALATE);
- a hop budget (24) ends a runaway loop with ESCALATE; at most 6 router model calls per incident, and a forced move
  (one legal agent) costs no call.

**Different incidents take different routes** ([`output_examples/`](../output_examples) shows each one):
- false alarm: monitor → investigator → check first → critic → VERIFY, with no planner call;
- a case the floor has handled well before: memory → playbook → critic, with no planner call;
- ambiguous: investigator before planning;
- critic rejection: back to the planner with the violations, or to candidates;
- verifier mismatch: back to candidates without the failed AGV, then the planner.

The rule-based router follows the same policy without a model. It is used in sample mode, when Nightshift is
down, and after the router-call budget. The *Incidents* tab draws the route as a graph with numbered hops, the
reason for each hop and who chose it, and can show the previous incident's route next to it. The original fixed
pipeline is kept for comparison: `run.py incident --linear`, `options.mode: "linear"` or `POST /run {"mode": "linear"}`.

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

Every external dependency sits behind an adapter: [`app/zetaris.py`](../app/zetaris.py) and [`app/llm.py`](../app/llm.py), wrapped by the circuit
breakers in [`app/runtime.py`](../app/runtime.py).
