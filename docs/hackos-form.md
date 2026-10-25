# HackOS submission form: paste-ready answers

Fill in the fields marked **[YOU]**. Check every claim against the final repository before you submit.

**Project name:** Shop-Floor OS

**Tagline:** AI agents plan, robots execute, humans judge.

**Track (exactly one):** 3, Reasoning Architecture

**Team members:** [YOU] (confirm the roster before creating the submission; the team locks at the first
submission)

**GitHub repository (public):** [YOU] (the repository has to be made public before the deadline)

**Demo video (1–4 min):** [YOU] (upload link)

**Slide deck:** [YOU] (link; the outline is in `docs/deck-outline.md`)

**Colab links:** not applicable (no Colab)

**Short description (≤ 50 words):**
Shop-Floor OS is a multi-agent incident-response and AGV-dispatch system for car assembly. Agents read plant
data through Zetaris, ground plans in a Meterless World Model, learn with H-MEM, plan with Nemotron, and pass a
safety critic that cites OSHA clauses, a simulated dry-run and a restraint gate, before a human approves.

**Long description:**
On an assembly line, a missing kit, a stalled AGV, a spill or a machine alarm costs cars and can put people at
risk. Supervisors must decide in seconds who to send, how robots should move, whether the alarm is real, and
whether acting is safe.

Shop-Floor OS handles each incident with a bounded loop of specialised agents:
- a rule-based reflex geofences the area in under 100 ms;
- a monitor separates real faults from sensor noise, using thresholds and real Bosch vibration recordings;
- H-MEM recalls lessons from past incidents;
- one federated Zetaris query builds a Meterless World Model of qualified people and healthy AGVs;
- Nemotron (via Meterless Nightshift) proposes a strict-JSON plan that must cite the governing clauses;
- a deterministic safety critic checks it against OSHA-based rules and cites the exact 29 CFR clause for every
  violation;
- the plan is dry-run in a 60-second simulation (A* with a reservation table);
- a restraint gate chooses ACT, VERIFY, SAFE_HOLD, ESCALATE or DO_NOTHING from confidence and cost;
- after execution, a verifier checks the world changed as predicted and re-plans if not.

A supervisor approves or rejects in the web UI. A lab tests Nemotron's policy ideas against the real incident
history with seeded, LLM-free replays and a bootstrap CI, and writes significant proposals back through
Zetaris. Everything is traced, runs as JSON in and JSON out, and works offline with `SAMPLE_MODE=true`.

**Sponsor technology explanation:**
- **Zetaris:**
  - The data layer, and the only data path (`app/zetaris.py`). Four Postgres sources are registered as `sf_fleet`,
    `sf_hr`, `sf_plant` and `sf_ops`, all in code through the SQL REST API.
  - Every incident starts with one federated Lightning SQL query across all four (stations, machines, workers
    and certifications, AGVs with live position, safety rules, cost history).
  - A Unified Semantic Layer, `floor` (9 business entities with keys, `app/usl.py`), models the plant once;
    the zone briefing reads through it.
  - The agents read on their own read-only login (role `SF_AGENT_READER`), so Zetaris itself refuses any write
    from them; *Ask the floor* writes guarded Spark SQL on it.
  - Lab proposals are written back with `INSERT INTO sf_ops.experiments`, and the agents' own steps, model calls
    and journal are synced into `sf_ops` every 30 s, so the agents can be questioned with SQL too.
- **Meterless:**
  - Three engines are implemented in Python from the open specs, each running a feature with its tests. The
    **World Model** grounds every plan: stable ids, provenance, facts such as `service_overdue`, and a `ground()`
    check that rejects invented ids (8 conformance checks ported to pytest). **H-MEM** holds the incident
    playbooks (lessons with outcome feedback) and the OSHA/SOP clause index the critic and planner cite (all 13
    official vectors pass). **Markovian** writes the shift handover from the whole journal in bounded chunks
    (67% fewer prompt tokens on a live three-shift run).
  - **Meterless Nightshift** is the only model provider. Each Nemotron model is called under its own app, pinned
    to one lane; every call records the requested and the answering model, and each Ed25519 receipt is verified
    when its answer is used (6,021 of 6,021 verify).
- **Cursor:** used as a development environment. The project rules are in `.cursor/rules/shopfloor.mdc` and
  `AGENTS.md`, and the model layer (`app/llm.py`) and its tests were built in Cursor. Claude Code was also used.
  [YOU: confirm or extend with your own Cursor usage.]
- **NVIDIA:** three Nemotron models through Nightshift, chosen per agent role (`config/models.json`).
  - `nvidia/nemotron-3-ultra-550b-a55b` plans, analyses evidence and proposes Lab changes.
  - `nvidia/nemotron-3.5-super-vl-120b-a12b` coordinates investigations, writes SQL and the shift handover, and
    backs up the Ultra roles.
  - `nvidia/nemotron-3.5-lightning-30b-a3b` routes the orchestrator, triages Sentinel signals, writes monitor
    summaries and re-checks the analyst's claims, with reasoning switched off where NVIDIA's guidance says the
    token budget is better spent on the answer.
  - The answering model is logged per request (`logs/llm.jsonl`, `logs/trace-<run_id>.jsonl`), and the
    *Nemotron at work* tab counts what Nemotron decided.
- **What was unique:**
  - Three Meterless engines work together: the World Model for truth about the plant, H-MEM for learned lessons
    and regulation, Markovian for the shift handover over the whole journal in bounded chunks.
  - The LLM is never trusted alone. Every plan is grounded, critic-checked with CFR citations, simulated, cost-
    gated, verified and human-approved, and any decision can be re-derived later with no model.

**Pre-existing components:** None. The repository was created on 23 October 2026, inside the build window.
Open-source used: the Meterless engine specs (Apache-2.0), ported to Python; public datasets (UCI AI4I 2020,
OSHA Severe Injury Reports, BLS SOII, Open-Meteo, Bosch CNC Machining, MovingAI MAPF, eCFR text), credited in the
README. [YOU: also declare any data or code you prepared before 22 October, if there is any.]

**How to run (for judges):**
- `pip install -r requirements.txt && python run.py run input_examples/example_1.json`. This needs no keys
  (sample mode).
- Or `docker build -t shopfloor-os . && docker run -p 8000:8000 shopfloor-os`, then open http://localhost:8000, or
  `curl -X POST http://localhost:8000/run -H "Content-Type: application/json" -d @input_examples/example_3.json`.
- The live setup is in the README.
