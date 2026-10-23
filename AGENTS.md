# Shop-Floor OS: agent instructions

Cursor is the main build tool for this project. These rules match `.cursor/rules/shopfloor.mdc`; keep the two
files in sync. They apply to any coding assistant (Cursor, Codex, Claude Code).

## Purpose
Shop-Floor OS is a multi-agent dispatch system for a fictional car final-assembly hall: AI agents plan, robots
(AGVs) execute, humans judge. Open Agent Hackathon 2026, Track 3 (Reasoning Architecture).
- **Data:** four Postgres databases (Docker container `shopfloor-pg`), registered in a local Zetaris instance as
  `sf_fleet`, `sf_hr`, `sf_plant` and `sf_ops`. The data is synthetic and anchored on real public sources; see
  `README.md` and `sf_ops.data_sources`.
- **Models:** Nemotron via Meterless Nightshift (`MODEL_BASE_URL`), with lanes `NEMOTRON_DEEP` and
  `NEMOTRON_FAST`.

## Repo layout
Folders marked (planned) do not exist yet; create them when the first module needs them.

| Path | What goes there |
|---|---|
| `app/data/` | Data generator (`generate.py`) and the optional rebuild of `data/seed/` (`fetch_sources.py`) |
| `app/sim/` | (planned) Deterministic floor simulation: AGV movement, incidents, time stepping |
| `app/agents/` | (planned) The reasoning agents and their Pydantic message models |
| `app/lab/` | (planned) Offline experiments and replays of incident history |
| `app/engines/` | (planned) Non-LLM engines: path planning, scheduling, rule checks |
| `app/zetaris.py` | The only data access layer |
| `app/llm.py` | (planned) The only model-call layer |
| `web/` | (planned) Demo UI |
| `tests/` | (planned) pytest tests |
| `logs/` | JSONL run logs (`*.jsonl` is git-ignored) |
| `data/seed/` | Committed real-data summaries the generator reads |
| `scripts/` | `start_postgres.sh`, `load_data.sh` |
| `run.py` | Command line entry point |

## Rules
1. **Secrets** come only from `.env` via python-dotenv (`load_dotenv()`). Never hard-code, print, log or commit
   a key, token or password, and never put one in a prompt or URL. To check a setting, test whether it is set.
2. **Model calls** all go through `app/llm.py` to Meterless Nightshift, using `NEMOTRON_DEEP` or
   `NEMOTRON_FAST` from `.env`. Never call NVIDIA or any other provider directly, and do not use the local lane
   without asking, because it can trigger a large model download.
3. **Data reads** all go through `app/zetaris.py` (`run_sql`). Use one federated query per incident. Run
   read-only SQL first (`SHOW`, `DESCRIBE`, `SELECT`). Send one statement per call, with no trailing semicolon
   and with qualified names (`sf_ops.incidents`). Ask before anything that creates, drops or writes. Syntax
   reference: `~/hackathon/getting-started/docs/guides/zetaris-lightning-sql-commands.md`.
4. **Reload** data with `bash scripts/load_data.sh`, then `.venv/bin/python -m app.zetaris refresh`.
5. **No LLM calls** inside `app/sim` or in lab replays. Both must be deterministic and reproducible (seeded).
6. **Agents exchange Pydantic models** and use strict JSON. Validate every model reply against its schema, and
   reject it or retry. Never pass free text between agents.
7. **Every agent step** appends one JSONL line to `logs/`, with timestamp, agent, step, inputs, output, model
   lane and latency. Never write secrets to it.
8. **Each new module** gets a pytest test in `tests/`. Tests must not need network access, Zetaris or
   Nightshift unless they are marked as integration tests.
9. **Prefer the standard library.** Ask before adding any dependency. Runtime dependencies go in
   `requirements.txt`; the optional data rebuild uses `requirements-data.txt`.
10. **Git is local only.** Commit locally when asked; never push, add a remote or use GitHub.
11. **Ask first** before installing packages, starting or removing containers, or downloading data. Never run
    `docker compose down -v` or touch Docker volumes. Leave `~/hackathon/zetaris-platform`,
    `~/hackathon/getting-started` and `~/hackathon/open-agent-hack` unchanged.

## Preflight (read-only)
```bash
.venv/bin/python -m app.zetaris "SELECT 1"            # Zetaris REST answers [['1']]
.venv/bin/python -m app.zetaris "SHOW DATASOURCES"    # sf_fleet, sf_hr, sf_plant, sf_ops
docker ps --format '{{.Names}} {{.Status}}'          # shopfloor-pg and the zetaris-* containers are up
.venv/bin/python run.py models                        # Nightshift model ids (needs Nightshift running)
```
Report a short pass/fail list, and stop if anything fails.

## Common commands
```bash
.venv/bin/python run.py context --zone FINAL          # one federated query plus a Nemotron summary
.venv/bin/python -m app.zetaris "<SQL>"               # one read-only statement
bash scripts/load_data.sh && .venv/bin/python -m app.zetaris refresh
.venv/bin/python -m pytest                            # once tests/ exists
```
