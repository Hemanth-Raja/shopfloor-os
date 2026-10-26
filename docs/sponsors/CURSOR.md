# Cursor in Shop-Floor OS

Part of the [sponsor technology docs](README.md). Back to the [README](../../README.md#cursor).

## What Cursor did
- **The project rules.** [`.cursor/rules/shopfloor.mdc`](../../.cursor/rules/shopfloor.mdc) holds the rules every coding assistant
  follows in this repo: secrets only from `.env`; every model call through Nightshift with a `role=`; every data
  read through `app/zetaris.py`; no model calls in the simulator or lab replays; strict JSON between agents; one
  JSONL log line per agent step; a test for each new module. [`AGENTS.md`](../../AGENTS.md) carries the same rules for
  other tools, and each file says at the top that it must stay in sync with the other. They were the first files committed
  after the data layer (commit `8ae0e43`, 24 Oct 2026, "Add Cursor project rules and AGENTS.md").
- **The model layer.** Cursor built [`app/llm.py`](../../app/llm.py), the Nightshift client: the OpenAI SDK pointed at
  `MODEL_BASE_URL` with the `meterless-app` header, the `deep` and `fast` lanes, `chat` and `chat_json` (a reply
  validated against a Pydantic schema, with one retry on a schema miss), the `logs/llm.jsonl` record of requested
  and answering model, and `LLMUnavailable`, the error the rest of the code catches to switch to safe mode. It also wrote its tests, [`tests/test_llm.py`](../../tests/test_llm.py), which run with no network
  (commit `049d069`, 24 Oct 2026, "Cursor build: app/llm.py model layer, tests/test_llm.py, run.py via app.llm").
  Every later model feature (per-role chains, breakers, receipts in [`app/models.py`](../../app/models.py)) is built on that client.

## How to check it
```bash
.venv/bin/python -m pytest -q tests/test_llm.py      # the model layer's offline tests
git log --oneline -- app/llm.py tests/test_llm.py .cursor/rules/shopfloor.mdc
```
Result on 26 Oct 2026: `6 passed`.

## Disclosure
Claude Code (Anthropic) wrote most of the code and documentation in this repository; Cursor wrote the model layer
above. The team reviewed and tested all of it (README *Disclosure*, item 4).
