# Models and Meterless engines

Part of the [Shop-Floor OS README](../README.md); the README has the summary of each section.

## Models: which model serves which agent
[`config/models.json`](../config/models.json) maps each agent role to a chain of models, chosen by capability. [`app/models.py`](../app/models.py) tries the
chain in order through Nightshift.

| Role | Model chain (first choice first) | Reasoning | Why |
|---|---|---|---|
| router (orchestrator), monitor, sentinel | Nemotron 3.5 Lightning 30B A3B → 3.5 Super VL | off, hedged | small, frequent decisions over a short state: lowest latency (3B active parameters) |
| planner, analyst, lab | Nemotron 3 Ultra 550B → 3.5 Super VL 120B → Lightning | model default (on) | the hardest reasoning: grounded multi-constraint plans with clause citations, findings over evidence, policy ideas |
| coordinator, sql_agent | Nemotron 3.5 Super VL 120B → Lightning | medium | splitting a question and writing Spark SQL over the catalogue |
| handover, handover_reflect | Nemotron 3.5 Super VL 120B → Lightning | model default (on) | the Markovian chunks and the final note |
| evidence_critic | Nemotron 3.5 Lightning → Super VL | default | a different model from the analyst, for an independent check; it can only drop claims |

The safety critic, dry run, restraint gate and executor are deterministic code and use no model.
Llama-3.3-Nemotron-Super 49B is in Nightshift too, but as a judge lane: it grades answers and refuses to answer a
request itself, so no role can use it directly.

- Each model has its own circuit breaker. After two failures it opens, and the next model in the chain answers at once.
- Every role has a latency budget (the call timeout) and a token budget.
- Every call records the **requested** and the **answering** model. Nightshift can serve a different model from
  the one requested.
- Every call also records the id of **Nightshift's signed receipt** for that request, matched on the response id.
- All of this appears in the trace (`model_call`), in `logs/llm.jsonl`, in the *Incidents* tab's agent details,
  and in the *Models* panel on *Nemotron at work*, which shows live latency and breaker states.

[`scripts/probe_models.py`](../scripts/probe_models.py) asks every NVIDIA model Nightshift lists for a one-word reply. It records who really
answered in [`data/model_probe.json`](../data/model_probe.json). **Nightshift, not the model id, decides the route.** For a named app, Nightshift
records the requested model but routes by that app's rule, which is why each model has its own app name
(`model_apps` in the config) with a one-lane allowlist in Nightshift's app policy. When NVIDIA's endpoint returns an
error or an empty reply, the chain moves on and the trace says so.

## Meterless engine: World Model
Shop-Floor OS implements the **Meterless World Model** engine in Python, in [`app/engines/world_model.py`](../app/engines/world_model.py). It is a
port of the spec in github.com/Meterless/Meterless `engines/world-model` (Apache-2.0), and a reference copy of the
spec and conformance runner can be fetched into `data/raw/meterless/`.
- **The engine:** an event-sourced store of entities, contexts and bitemporal relationships, with stable
  content-derived ids, provenance on every write, banded name resolution (auto-merge at 0.85 or above, review queue
  from 0.82), idempotent ingest, an ordered audit log, and graph, facts, snapshot and timeline views.
- **The feature:** `run.py context --zone FINAL` sends one federated Zetaris query, and
  [`app/engines/shopfloor_world.py`](../app/engines/shopfloor_world.py) builds the zone's world from it:
  - incidents, stations, machines, workers, AGVs and Bosch vibration recordings, with their links;
  - grounded checks, such as whether the responder's lockout/tagout was valid and whether the AGV's service is
    overdue.

  The Nemotron prompt receives the world model's structured context payload, not raw rows. `ground()` lets a
  planner reject entity ids that are not in the world.
- **Tests:** [`tests/test_world_model.py`](../tests/test_world_model.py) ports the spec's conformance checks (sid-001, sid-002, band-001,
  prov-001, idem-001, idem-002, bitemp-001 and audit-001) plus the reference smoke tests.
  [`tests/test_shopfloor_world.py`](../tests/test_shopfloor_world.py) covers the shop-floor builder. Run them with `.venv/bin/python -m pytest`.

## Meterless engine: H-MEM
The incident playbooks ([`app/engines/playbooks.py`](../app/engines/playbooks.py)) run on a Python port of the **Meterless H-MEM** engine,
in [`app/engines/hmem.py`](../app/engines/hmem.py). It follows the spec in github.com/Meterless/Meterless `engines/hmem` (Apache-2.0).
- Each critic lesson is stored as an H-MEM record with its source incident, tags and a confidence score.
- Before planning, the orchestrator recalls lessons with H-MEM's 8-signal hybrid ranking. Every outcome then
  feeds back into the record: helpful when the lesson held, not_helpful when the rule was broken again anyway.
  All of it goes through the append-only trust ledger.
- Sleep, conflict resolution and dreaming are implemented with the spec's guardrails.
- [`tests/test_hmem.py`](../tests/test_hmem.py) runs all 13 official conformance vectors, copied unchanged into [`tests/fixtures/`](../tests/fixtures).

## Meterless engine: Markovian (shift handover)
The shift handover ([`app/handover.py`](../app/handover.py)) runs on a Python port of the **Meterless Markovian** engine, in
[`app/engines/markovian.py`](../app/engines/markovian.py), following the Meterless "Markovian Agent Engine" reference (headless, v1.0).
- A shift writes hundreds of journal lines. They are cut into slices of 40 compact lines; each chunk sends Nemotron
  3.5 Lightning only a 200-character goal summary, the carried state and one slice, never the earlier slices.
- The model ends each chunk with `[STATE_CHECKPOINT]` and a state summary (counts, open incident ids, risks);
  `[TASK_COMPLETE]` ends the chain. The carryover goes through the spec's cascade in strict order: explicit state,
  model compression, key-phrase extraction, tail truncation, so a model outage still leaves usable state.
- Nemotron 3.5 Super VL writes the final note from the carried state (the spec's reflection step); a failed
  reflection keeps the partial output.
- Every run is a record in `logs/markovian.jsonl`: per-chunk prompt size, carryover size and which compression tier
  was used. The command prints the real prompt tokens against what re-sending the history would have cost
  (characters / 4, the spec's estimate, not a tokenizer).
- Measured on a live run over three shifts (255 journal events, 5 chunks, Nemotron 3.5 Super VL): 8,326 prompt
  tokens sent, against 25,042 if each step had re-sent the earlier slices and answers (67% saved, chars/4 estimate).
  The carried state never went above about 260 tokens.
- [`tests/test_markovian.py`](../tests/test_markovian.py) checks config bounds, the marker protocol, the cascade order, the spec's token
  formulas, abort and reflection failure, and the handover slicing (no network).
