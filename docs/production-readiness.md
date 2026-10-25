# Production readiness: the agent failure stack, checked

A review of Shop-Floor OS against a production checklist for agentic systems (data federation, execution runtime,
orchestration, system defenses and the common failure modes). Status: **covered** (in code and tested), **gap**
(fixed in this pass, listed at the end), or **limit** (stated honestly, not fixed).

## One correction to the reference architecture
In this hackathon the sponsor roles are: **Zetaris** is the federated data layer; **Meterless** provides the engines
(World Model, H-MEM, Markovian) and **Nightshift**, its local runtime that serves every model call (routing, lanes,
signed receipts). Nightshift is not a batch orchestrator of Manager/Dev/QA daemons. In Shop-Floor OS the
orchestration, the role separation and the verification gates are our own code (`app/dynamic.py`, the critic, the
human gate), which is what Track 3 judges.

## 1. Data and governance layer (Zetaris)
| Requirement | Status | Where |
|---|---|---|
| Query in place, no copy into a vector store | covered | four Postgres sources federated as `sf_*`; one federated query builds the World Model |
| Token firewall: filter and aggregate at the source | covered | SQL agent aggregates in Zetaris; 15 preview rows per result, 200-row cap; World Model payloads, not tables |
| Do not overload operational databases | covered | read cache with TTL, cleared on writes; row limits; Zetaris breaker and 60 s timeout |
| Read-only by construction | covered in code and in Zetaris; agent login **waits on one UI step** | `sqlguard.py` allows one `SELECT` on whitelisted tables, and agent reads use their own Zetaris login (`ZETARIS_AGENT_API_KEY`) whose only role, `sf_agent_reader`, has SELECT on the four sf_* datasources (`python -m app.zetaris reader-role`). The operator login is kept for register/refresh and the Lab write. Health shows which login agents use |
| Data freshness during a long run | **gap, fixed** | the plant snapshot was loaded once at server start; it now refreshes on a timer and falls back to the last good snapshot |

## 2. Execution and state (Meterless engines, Nightshift)
| Requirement | Status | Where |
|---|---|---|
| Model routing per role, fallback, pinning | covered | `config/models.json` chains, per-model breakers, self-check, drift warnings, hedging |
| Local models for cheap or private steps | **limit** | only cloud Nemotron is served on our key; a local model needs a download, so it waits for your OK |
| Persistent world model and memory | covered | Meterless World Model (facts), H-MEM (lessons, clauses); Nightshift's own answer memory switched off |
| Long runs without context bloat | covered | each incident starts a fresh context; Markovian handover carries compressed state (67% fewer prompt tokens) |
| Replay, resume | covered | per-step traces, hash-chained journal with crash recovery, SAMPLE_MODE replays, the replay site |

## 3. Orchestration and enforcement
| Requirement | Status | Where |
|---|---|---|
| Role separation | covered | router, monitor, candidates, planner, critic, dry run, restraint, gate, executor, verifier, investigators |
| Programmatic gates, not prompts | covered | legal moves in code, deterministic critic with default-deny, grounding, dry run, runtime invariant monitor |
| Isolated workspace before acting | covered | every plan runs in a cloned sim (dry run) before it touches the floor |
| Human-readable dated shift log | **gap, fixed** | the handover note is now also written to `logs/shift-YYYY-MM-DD.md` |
| Ambiguous tasks go to a person | covered | ESCALATE / VERIFY, human gate, approval-timeout policy |

## 4. System-level defenses
| Defense | Status | Where |
|---|---|---|
| Circuit breakers on time, tool calls, invalid output | covered | per-model and Nightshift-wide breakers, 240 s incident deadline, hop budget 24, 6 router calls, 3 critic rounds, JSON repair then re-ask |
| Idempotency and transaction boundaries | covered for dispatch; **gap, fixed** for Lab writes | fencing tokens and idempotency keys on dispatch; a retried Lab INSERT could duplicate a row, now checked first |
| Tamper-evident audit tied to identity and data version | covered, **gap, fixed** for data version | hash-chained journal and approvals (HMAC); each trace now records the plant snapshot it saw. Approver identity is a typed name (**limit**: no login) |
| Checkpointing, fail safe on crash | covered | journal recovery: interrupted incidents go to a person, leases voided, tokens move on |

## 5. The agent failure stack
| Failure mode | Status | How it is handled |
|---|---|---|
| 1. Tool-call and schema drift, partial results | covered | strict Pydantic on every reply; SQL errors go back to the agent for one retry; failed evidence drops the claims that cite it; startup model self-check |
| 2. State corruption, cascading bad reads | covered, plus the freshness fix | numbers must appear in the cited evidence; grounding against the World Model; the critic reads the database rules, not model output |
| 3. Handoff loss, endless loops | covered | agents pass typed objects, not summaries; every loop is bounded; the router cannot skip the critic or the gate |
| 4. Blast radius, permissions | covered in code, **limit** in the database | read-only SQL guard; the only writes are journal/log replicas and Lab proposals; a person approves anything risky |
| 5. Memory drift, self-grading | covered | lessons come only from the deterministic critic's findings, confidence drops when a lesson does not hold; Nightshift's answer memory off |
| 6. Economic exhaustion | covered, **gap, fixed** | retries, caches, AIMD limits, deadlines; now also a paced model-call budget (token bucket, at most a quarter of the hour's calls in a burst). Low-value calls are shed first (Monitor summary, then router and evidence critic); a shed call is pacing, not an outage, so the critic-checked rules plan still runs on its own |
| Prompt injection through free text | **gap, fixed** | the reporter's note goes to the planner as quoted data; text that tries to instruct the agents is flagged and kept out of the prompt |

## What still needs your OK
- A read-only Zetaris role for the agents (database-level permission bounding, not only the SQL guard).
- A local model in Nightshift for the router or the PII-sensitive steps (needs a model download).
- A login for approvals (identity beyond a typed name). For a local demo this stays a stated limit.
