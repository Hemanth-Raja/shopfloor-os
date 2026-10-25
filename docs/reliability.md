# Reliability: retrieval, failures and what broke

Part of the [Shop-Floor OS README](../README.md); the README has the summary of each section.

## Retrieval, context limits and failure handling
**Retrieval (RAG), without a vector database.** The models never get raw tables or whole documents; each agent
retrieves what it needs:
- *Regulations:* OSHA 29 CFR 1910 excerpts (eCFR) and plant SOPs are H-MEM records ([`app/engines/citations.py`](../app/engines/citations.py)).
  The planner is shown the clauses H-MEM ranks for the incident kind, the critic attaches the exact clause to every
  violation, and cited references are resolved back to their text (unknown references are flagged).
- *Experience:* H-MEM lessons and playbooks are recalled per incident kind and station before planning.
- *Facts:* the World Model, built from one federated Zetaris query, gives grounded entities; `ground()` rejects
  ids that do not exist.
- *History:* the investigator and Ask the floor retrieve through guarded SQL on Zetaris and a search of our own
  traces.

**Context limits.** The largest prompt in our logs is about 7.5k tokens. Nemotron 3.5 Lightning has a 262k-token
window and Super VL 1M. Limits are enforced in code anyway:
- before each call, [`app/models.py`](../app/models.py) estimates the prompt and skips any model where prompt plus `max_tokens` would
  exceed half its window (`context_tokens` and `context_fraction` in [`config/models.json`](../config/models.json));
- each role has a token budget (`max_tokens`) and a time budget (`budget_s`);
- prompts carry compact state: World Model payloads, at most 15 rows per SQL result, SQL capped at 200 rows, and
  the last 200 model-log lines;
- the orchestrator has a hop budget (24) and at most 6 router calls per incident.

**When a model fails.**
1. A timeout, HTTP 5xx or 429, an empty reply or a reply that fails schema validation (after one retry) moves to
   the next model in the role's chain.
2. Each model's circuit breaker opens after two failures, and later calls skip it at once until a cooldown.
3. If every model fails, the Nightshift-wide breaker opens. The router falls back to rules, the planner is replaced
   by safe mode (a rule-based plan plus ESCALATE, so a person decides), and the investigator uses rule-based
   stand-ins.
4. Each fallback is visible: the trace records the requested and answering model, the models tried and why, and
   `router` and `safe_mode` steps.
5. `SAMPLE_MODE=true` runs the whole system offline from recorded replies.

Tests cover each path: [`tests/test_models.py`](../tests/test_models.py), [`tests/test_dynamic.py`](../tests/test_dynamic.py), [`tests/test_agent_loop.py`](../tests/test_agent_loop.py) and
[`tests/test_health.py`](../tests/test_health.py).

**When retrieval goes wrong.** Retrieval is treated as untrusted input; nothing retrieved is acted on unchecked.

| Retrieval failure | What happens | Where |
|---|---|---|
| The SQL agent writes unsafe or invalid SQL | The guard allows one read-only `SELECT` on whitelisted `sf_*` tables; the rejection reason or Zetaris error goes back for one retry | [`app/investigate/sqlguard.py`](../app/investigate/sqlguard.py), [`app/investigate/investigator.py`](../app/investigate/investigator.py) |
| A query fails or returns nothing useful | The evidence is marked failed; any claim citing it is dropped with the reason | [`investigator.py`](../app/investigate/investigator.py) (evidence critic) |
| The analyst states a number the data does not contain | Every number in a claim must appear in the cited evidence, or the claim is dropped; a Lightning critic then reviews what is left | same |
| Clause retrieval ranks the wrong regulation | The safety critic is code over `sf_ops.safety_rules`, not over what was retrieved, so a bad ranking cannot approve a plan; a rule the code cannot check blocks automatic action (default-deny) | [`app/agents/safety_critic.py`](../app/agents/safety_critic.py) |
| The planner cites a clause that does not exist | Unknown references are flagged in the trace and dropped from the plan's citations | [`app/engines/citations.py`](../app/engines/citations.py) |
| The model names a person or robot that does not exist | World Model `ground()` rejects the id (GROUNDING violation) and the planner gets the reason | [`app/agents/planner.py`](../app/agents/planner.py) |
| A cached read is stale | Read-only Zetaris results expire (TTL), and any write clears the cache, so agents never read their own stale state | [`app/cache.py`](../app/cache.py) |
| Retrieved lessons are wrong | H-MEM lowers a lesson's confidence when its outcome is not_helpful, and every plan still goes through the critic | [`app/engines/playbooks.py`](../app/engines/playbooks.py) |

## Resilience: what happens when things go wrong
| Failure | What the system does | Where |
|---|---|---|
| NVIDIA endpoint returns 5xx, 429, an empty reply or times out | Retries on the same model with full-jitter exponential backoff, inside the incident's deadline; then the next model in the role's chain | [`app/models.py`](../app/models.py), [`app/resilience.py`](../app/resilience.py) |
| A model gets slow or overloaded | A per-model AIMD concurrency limit halves on errors or very slow replies and grows back slowly; the router, monitor and Sentinel hedge to the next model after 1.5x their median latency | same |
| A model keeps failing | Its circuit breaker opens; half-open lets exactly one probe through | [`app/agents/health.py`](../app/agents/health.py) |
| Nightshift serves a different model than pinned | Startup self-check and live drift rates; the effective chain skips it; Health warns | [`app/models.py`](../app/models.py) |
| The model's JSON is broken | Mechanical repair first, then one re-ask, then the next model | [`app/resilience.py`](../app/resilience.py) |
| The model is slow or down during an incident | Anytime planning: the optimal-assignment plan (v1) is ready in milliseconds; the model's v2 replaces it only if it passes the critic and the dry run and scores better; with Nightshift down, v1 is used and a person approves | [`app/agents/assign.py`](../app/agents/assign.py), [`app/dynamic.py`](../app/dynamic.py) |
| A plan breaks a safety rule | The critic rejects it with the clause; a rule it cannot check blocks automatic action (default-deny) | [`app/agents/safety_critic.py`](../app/agents/safety_critic.py) |
| Something unsafe happens anyway on the floor | The runtime invariant monitor stops the AGV (AGV in a fence with a person, fast at a crossing) or escalates (no lockout/tagout in a robot-cell area); the fleet layer caps AGVs at 0.5 m/s on crossings whatever the plan says | [`app/sim/invariants.py`](../app/sim/invariants.py), [`app/sim/sim.py`](../app/sim/sim.py) |
| Two incidents want the same person or robot | Leases with fencing tokens; higher risk pre-empts, the loser re-plans; a stale holder's command is refused; repeated dispatches are idempotent | [`app/autonomy/leases.py`](../app/autonomy/leases.py), [`app/connectors.py`](../app/connectors.py) |
| The process crashes mid-shift | The hash-chained journal is replayed: interrupted incidents go to a person, their leases are void, tokens resume above the last one | [`app/journal.py`](../app/journal.py) |
| Someone edits the logs | Journal and approvals are hash chains (HMAC with `JOURNAL_HMAC_KEY`); `run.py audit` finds the first bad line | [`app/journal.py`](../app/journal.py) |
| Zetaris is slow or down | Breaker, 60 s timeout, read cache cleared by any write; sample mode runs offline | [`app/runtime.py`](../app/runtime.py), [`app/cache.py`](../app/cache.py) |
| Sensors drift slowly | EWMA baselines from history plus CUSUM raise an early warning before any fixed threshold; hard limits never adapt | [`app/autonomy/detectors.py`](../app/autonomy/detectors.py) |

Every one of these has tests, including seeded property-based fuzzing of the critic and the runtime monitor
([`tests/test_fuzz_safety.py`](../tests/test_fuzz_safety.py), `FUZZ_SEEDS=25` for a longer run). `python run.py eval` measures them: planted unsafe
plans, random plans judged only by the runtime monitor, Monitor calibration and, with `--live`, a Nemotron-only
baseline and 3x stability. Results: [docs/eval.md](eval.md).

## What broke and how we fixed it
Things that failed during the build, found by tests, fuzzing, the runtime monitor or the eval, and what we changed.

| What broke | How we found it | Fix |
|---|---|---|
| The rule-based fallback plan sent a worker without valid lockout/tagout to a robot-cell fault, and a worker without a torque-audit skill to a torque fault | `run.py eval`: 12 of 54 safe-mode base plans were unsafe (the critic caught all 12, so nothing reached the floor) | The fallback now picks the first candidate who meets what the critic will ask for; 0 of 54 unsafe since ([`app/agents/planner.py`](../app/agents/planner.py)) |
| A plan the critic approved could still cross a pedestrian walkway too fast, because the executed path differed from the critic's estimate | Seeded property-based fuzzing ([`tests/test_fuzz_safety.py`](../tests/test_fuzz_safety.py)) | The fleet layer caps AGVs at 0.5 m/s on crossings whatever the plan says; the critic and the runtime monitor stay as separate layers |
| The reflex left a person without lockout/tagout inside a robot-cell repair area, and responders walked back into locked-out areas | The runtime invariant monitor ([`app/sim/invariants.py`](../app/sim/invariants.py)) | The reflex moves such people out; workers route around locked-out areas; new critic rule R-HAZARD-ZONE |
| Planner calls timed out at 20 s | Live runs: the deep lane reasons for 10-40 s before answering | Planner budget 90 s inside a 240 s incident deadline, plus anytime planning: the optimal-assignment plan is ready in milliseconds and the model's plan only replaces it if safer and better |
| Llama-3.3-Nemotron-Super 49B and, at first, Nemotron 3 Ultra 550B never answered our app (the 49B is a judge lane that refuses requests; Ultra had no lane pinned to our app) | Live calls and the startup self-check | Ultra got its own pinned app (`shopfloor-os-ultra`) and now heads the planner, analyst and Lab chains; the self-check skips any model that fails or is silently served by another |
| A chain falling back to a second model was refused by the first model's Nightshift lane pin | Live fallback test | Each model goes out under the Nightshift app whose lane allows it ([`config/models.json`](../config/models.json) `model_apps`) |
| Free endpoints throttled (429, empty replies) when several incidents ran at once | Live shifts | Retries with full-jitter backoff, a per-model AIMD limit, hedged requests, breakers; live demos and eval runs pin concurrency at 2 (`MODEL_CONCURRENCY_MAX`) |
| Headless live shifts ticked faster than the models answered, so incidents were cut off | Live shift runs | The floor clock runs at the UI's pace and the shift waits (capped) for open incidents |
| Broken JSON from the model | Schema validation on every reply | Mechanical repair first, then one re-ask, then the next model |
| The shift handover came back empty (HTTP 502 "empty_response") | Live run; a raw request showed Nightshift's error body | The model reasons before it answers: with a 1,500-2,000-token budget it spent everything thinking. The handover now runs on Super VL with 6,000 tokens and a direct per-chunk prompt; it also read only 3 of 5 slices once, because the model signalled completion early, so the engine now reads every slice |
| Ask the floor answers recorded for the offline demo did not replay | Sample-mode check after recording | Live rows and their JSON copy differed in number formatting, so later prompts hashed differently; record mode now hands the agents the rows exactly as a replay will see them |
| Nightshift's own answer memory was adding earlier answers to our requests (118 of the 263 receipts before 24 Oct 23:53 UTC); a bare question with no data came back naming a real incident id | Receipts (`hmem.injected`) after a suspicious answer; Sanctions Desk documented the same setting | Switched off for the whole runtime (`hmemKnowledge: false`, Nightshift admin setpoints, previous setpoints were the defaults). Every receipt since shows no added memory; the live eval was re-run afterwards and those are the numbers above. Earlier recorded runs may include such memories |
| With the router's first model down, the second model was never tried, so one failed model put the whole incident into safe mode | Chaos test `python run.py chaos` | `models._race` now gives the second model its turn when the first fails before the hedge starts |
| The server crashed when Zetaris was down at start | Chaos test | It starts degraded on the offline plant, says so in Health and the UI, and keeps using Nightshift |
| Port 8000 is taken by Windows on the demo PC | First live demo | The server takes `--port`; docs use 8100 |
