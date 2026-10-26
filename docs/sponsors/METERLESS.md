# Meterless in Shop-Floor OS

Part of the [sponsor technology docs](README.md). Back to the [README](../../README.md#meterless-engines).

Three Meterless engines run features of the product, each ported to Python from the Meterless specs
(github.com/Meterless/Meterless, Apache-2.0) and checked by its conformance tests. Meterless Nightshift is the only
model provider: every model call goes through it, and every answer carries a signed receipt that we verify.

## The engines
| Engine | What it does here | Feature it powers | File | Conformance test |
|---|---|---|---|---|
| **World Model** | Stable, content-derived ids, provenance on every write, facts such as `service_overdue`, and `ground()`, which rejects ids that do not exist | Every plan: the candidates agent picks people and AGVs from it, and the planner's ids must ground in it. `run.py context` builds a zone's world from one federated Zetaris query | [`app/engines/world_model.py`](../../app/engines/world_model.py) (761 lines), built by [`plant_world.py`](../../app/engines/plant_world.py) and [`shopfloor_world.py`](../../app/engines/shopfloor_world.py) | [`tests/test_world_model.py`](../../tests/test_world_model.py): the spec's 8 conformance checks (sid-001, sid-002, band-001, prov-001, idem-001, idem-002, bitemp-001, audit-001) plus the reference smoke tests: **19 passed** |
| **H-MEM** | Hierarchical memory with an 8-signal hybrid ranking and an append-only trust ledger; outcomes move a lesson's confidence up or down | The incident playbooks (re-use a proven response with no model call), the critic's lessons recalled before planning, and the OSHA/SOP clause index that the planner and critic cite | [`app/engines/hmem.py`](../../app/engines/hmem.py) (516 lines), used by [`playbooks.py`](../../app/engines/playbooks.py) and [`citations.py`](../../app/engines/citations.py) | [`tests/test_hmem.py`](../../tests/test_hmem.py): all 13 official vectors, copied unchanged into [`tests/fixtures/`](../../tests/fixtures): **13 passed** |
| **Markovian** | Reads a long input in bounded chunks; each chunk sees only the goal summary, the carried state and one slice, with the spec's compression cascade for the carryover | The shift handover (`run.py handover`): a whole shift's journal read in slices of 40 lines, then a handover note | [`app/engines/markovian.py`](../../app/engines/markovian.py) (319 lines), used by [`app/handover.py`](../../app/handover.py) | [`tests/test_markovian.py`](../../tests/test_markovian.py): config bounds, the marker protocol, the cascade order, the spec's token formulas, abort, reflection failure and handover slicing: **7 passed** |

Run the three suites (no network, Zetaris or Nightshift):
```bash
.venv/bin/python -m pytest -q tests/test_world_model.py tests/test_hmem.py tests/test_markovian.py
```
Result on 26 Oct 2026, in a fresh venv: `39 passed in 0.04s`.

**Measured effect of Markovian.** On a live three-shift handover (255 journal events, 5 chunks, Nemotron 3.5 Super
VL), the chunks sent 8,326 prompt tokens, against 25,042 if each step had re-sent the earlier slices and answers:
67% fewer, by the spec's characters/4 estimate. In sample mode, `run.py handover --shifts 3` on the local journal
read 159 events in 3 chunks with 5,840 prompt tokens against 10,930 (47% fewer). That run used the rule-based
stand-in for the model, so only the chunking is real there.

**How they work together.** The World Model grounds ids and facts, H-MEM ranks both lessons and regulatory
clauses, and Markovian reads the whole journal for the handover. A critic violation becomes an H-MEM lesson, and
the outcome of the next incident raises or lowers its confidence (see the diagram under
[How the agents work together](../../README.md#how-the-agents-work-together)).

More detail, including the port notes: [docs/models-and-engines.md](../models-and-engines.md).

## Nightshift: the only model provider
- **One client.** Every model call goes through [`app/llm.py`](../../app/llm.py) (an OpenAI-compatible client pointed at
  `MODEL_BASE_URL`) and [`app/models.py`](../../app/models.py), which picks the model chain for the agent's role from
  [`config/models.json`](../../config/models.json). The NVIDIA key stays inside Nightshift; the app holds only Nightshift's runtime token in
  `.env`. No code calls NVIDIA or any other provider directly.
- **One app per model.** Nightshift routes by app name, so each model is called under its own app, pinned in
  Nightshift's app policy to exactly one lane: `shopfloor-os-ultra` → Nemotron 3 Ultra 550B, `shopfloor-os` →
  Nemotron 3.5 Super VL 120B, `shopfloor-os-fast` → Nemotron 3.5 Lightning 30B. A startup self-check asks each one
  which model answers, and a role's chain skips any model that is not really served.
- **Requested vs answering model, per call.** `logs/llm.jsonl` records both for every call. Of 3,603 calls from
  23 to 26 Oct 2026, 100 were answered by a different model from the one requested (49 of them calls on the generic
  `fast` lane that Super VL answered). The UI's *Nemotron at work* tab shows the split.
- **Resilience on top.** Per-model circuit breakers, retries with backoff, hedged requests for small roles, a
  latency and token budget per role, and safe mode (a rules plan and ESCALATE) when every model is down. All 15
  failure scenarios in [docs/chaos.md](../chaos.md) are tested against a fake Nightshift.

## Signed receipts
Nightshift signs a compute receipt for every request with Ed25519; the signing key never leaves the daemon.
- **At call time.** Each call records the id of its receipt, matched on the response id, and the receipt's
  signature is verified when the answer is used ([`app/models.py`](../../app/models.py) `RECEIPT_OK`, with
  `verify_envelope` from [`app/receipts.py`](../../app/receipts.py)).
- **Afterwards.** `run.py receipts --verify` re-checks every receipt for the `shopfloor-os*` apps against the
  public key each carries and reports the signing key fingerprints, so a key change would show up as a second
  fingerprint. The Ed25519 check is a pure-Python implementation ([`app/crypto/ed25519.py`](../../app/crypto/ed25519.py)), with no new
  dependency.
- **Result on 26 Oct 2026:** `6215/6215 Nightshift receipts for shopfloor-os* verify (Ed25519), signing key(s):
  {'bc1d94b41e48b872': 6215}`. This needs Nightshift's receipts file on the machine, so it does not run in a fresh
  clone.
- **A real trace with receipts:** [docs/live-run/](../live-run/README.md) holds incident `SIM-42-008` as recorded live,
  with its four Nightshift calls, each with `receipt_verified: true`.
