# NVIDIA Nemotron in Shop-Floor OS

Part of the [sponsor technology docs](README.md). Back to the [README](../../README.md#meterless-nightshift-and-nvidia-nemotron).

Every model in Shop-Floor OS is an NVIDIA Nemotron model, served through Meterless Nightshift. Nemotron proposes
plans and routes the orchestrator; deterministic code decides what is safe. The safety critic, dry run, restraint
gate and executor use no model.

## Which model serves which role, and why
From [`config/models.json`](../../config/models.json): each role tries its chain in order (first choice first).

| Role | Chain | Reasoning | Max tokens | Budget | Why |
|---|---|---|---|---|---|
| router (the orchestrator) | Lightning 30B → Super VL | off | 3,000 | 30 s | A small decision over a short state, many times per incident: the 3B-active model for the lowest latency |
| monitor | Lightning 30B → Super VL | off | 3,000 | 30 s | A one-line summary for the supervisor; the classification itself is rules |
| sentinel (triage) | Lightning 30B → Super VL | off | 3,000 | 30 s | A quick yes/no with a reason for ambiguous signals only; rules decide the clear ones |
| planner (v2) | Ultra 550B → Super VL → Lightning | model default (on) | 4,000 | 120 s | Grounded multi-constraint plans with clause citations |
| analyst | Ultra 550B → Super VL → Lightning | model default (on) | 6,000 | 180 s | Correlates evidence into cited findings |
| lab | Ultra 550B → Super VL → Lightning | model default (on) | 6,000 | 120 s | Proposes one testable floor change from history |
| coordinator | Super VL → Lightning | medium | 3,000 | 90 s | Splits an open question into sub-tasks over the table catalogue |
| sql_agent | Super VL → Lightning | medium | 3,000 | 90 s | Writes Spark SQL for Zetaris |
| handover, handover_reflect | Super VL → Lightning | model default (on) | 6,000 | 150 s / 120 s | Markovian chunks and the final note |
| evidence_critic | Lightning → Super VL | model default | 4,000 | 120 s | A different model from the analyst, for an independent check; it can only drop claims |

Full names: `nvidia/nemotron-3-ultra-550b-a55b`, `nvidia/nemotron-3.5-super-vl-120b-a12b`,
`nvidia/nemotron-3.5-lightning-30b-a3b`.

**Reasoning budget choices.** Following NVIDIA's guidance that reasoning and the answer share the token budget:
reasoning is off for the small, frequent decisions (router, monitor, Sentinel triage), so they answer in one
short reply; medium for the coordinator and the SQL agent; and the model default, with a larger budget, for the
planner, analyst, Lab and handover. The handover budget was raised to 6,000 after the model spent a
1,500 to 2,000 token budget entirely on reasoning (noted in the config).

## Calls, tokens and latency per role
Computed from `logs/llm.jsonl` on this machine: **3,603 calls from 23 Oct 2026 22:17 UTC to 26 Oct 2026 08:52
UTC**, development and live runs together. No prompts are stored in that log. `deep` and `fast` are calls made
by lane rather than role (for example `run.py context` and early runs). Latency is wall-clock per call as the app
saw it.

| Role | Calls | Prompt tokens | Completion tokens | p50 latency | p95 latency | Answering model (calls) |
|---|---|---|---|---|---|---|
| router | 1,724 | 1,022,040 | 1,121,744 | 6.4 s | 18.0 s | Lightning 1,224 · Super VL 500 |
| monitor | 886 | 56,829 | 608,400 | 7.6 s | 18.3 s | Lightning 568 · Super VL 318 |
| planner | 408 | 588,139 | 473,562 | 13.8 s | 45.9 s | Super VL 340 · Lightning 37 · Ultra 31 |
| sentinel | 168 | 54,941 | 139,597 | 9.1 s | 19.5 s | Super VL 112 · Lightning 56 |
| sql_agent | 120 | 111,033 | 65,905 | 0.6 s | 17.9 s | Super VL 115 · Lightning 5 |
| deep (lane) | 88 | 243,054 | 121,960 | 14.5 s | 63.8 s | Super VL 88 |
| analyst | 54 | 173,879 | 95,655 | 21.7 s | 52.7 s | Super VL 42 · Lightning 10 · Ultra 2 |
| fast (lane) | 49 | 89,273 | 46,123 | 10.8 s | 55.7 s | Super VL 49 |
| coordinator | 38 | 31,343 | 26,925 | 0.1 s | 18.4 s | Super VL 35 · Lightning 3 |
| evidence_critic | 30 | 59,756 | 44,393 | 13.9 s | 33.7 s | Super VL 15 · Lightning 15 |
| lab | 19 | 22,302 | 17,577 | 4.5 s | 45.1 s | Super VL 14 · Lightning 3 · Ultra 2 |
| handover | 16 | 32,791 | 28,093 | 17.5 s | 46.8 s | Super VL 11 · Lightning 5 |
| handover_reflect | 3 | 2,951 | 8,497 | 41.1 s | 41.1 s | Super VL 3 |

What the table shows:
- **Ultra answered few planner calls (31 of 408).** Ultra was pinned on the last build day, so most planner calls
  before that went to Super VL (README *Limitations*).
- **Requested is not always answering.** In 100 of the 3,603 calls Nightshift served a different model from the
  one requested; every one is logged with both names.
- **The sql_agent and coordinator medians are under 1 s.** We did not investigate why for this write-up.
- **Many router and monitor calls went to Super VL**, the second model in their chain. The log does not record
  why for each call (an open breaker, a failed call, or an earlier version of the config).

A live example with its receipts: [docs/live-run/llm-SIM-42-008.jsonl](../live-run/llm-SIM-42-008.jsonl), the four calls of incident
`SIM-42-008` on 26 Oct 2026. Lightning classified the alarm (0.09 s) and routed two hops, and Ultra wrote a v2 plan
that the safety critic rejected.

## Nemotron alone vs the full loop
From [docs/eval.md](../eval.md), section 3 (live, through Nightshift; 27 planner calls: 9 scenarios × 3 repeats). The
planner model in that run was Super VL, measured before Ultra was pinned.

| | Nemotron alone (first plan, no critic) | Full loop |
|---|---|---|
| Plans that were unsafe or ungrounded | **6 of 27 (22%)** | **0 executed** |
| What happened to them | would have run as written | the critic sent each back with its clause; Nemotron fixed all 6; 0 fell back to the rule plan |

Section 4 of the same file: the same incident planned 3 times gave identical assignees in all 9 scenarios.

So Nemotron adds the routing, re-planning after the critic, triage, investigation and the Lab's ideas, and the
loop around it makes it safe to act on. On the refine step's score, Nemotron's v2 plan seldom beats the optimal
rules plan (README *Limitations*).

## How a judge can see which model answered
- **Per incident:** `models` in every `POST /run` response, and the `model_call` of each step in
  `logs/trace-<run_id>.jsonl`, with requested model, answering model and latency.
- **Per call:** `logs/llm.jsonl` (requested and answering model, latency, tokens, receipt id).
- **In the UI:** the *Incidents* tab shows the answering model and latency on each agent's details; *Nemotron at
  work* (`GET /api/nemotron?hours=24`) counts calls and tokens per role, which model answered, hops chosen by
  Nemotron vs forced by code, plans it wrote and refinements kept or rejected.
- **Proof:** `run.py receipts --verify` checks Nightshift's Ed25519 receipt for every call (6,215 of 6,215 on 26
  Oct 2026). See [METERLESS.md](METERLESS.md#signed-receipts).
- **In sample mode** the replayed answers are marked `(replay)` after the model name, and rule-based stand-ins say
  `rule-based`.
