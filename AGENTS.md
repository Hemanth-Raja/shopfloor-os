# Agent instructions

Shop-Floor OS is a multi-agent dispatch system for a fictional car assembly hall. Its data is synthetic and
anchored on real public sources. The data lives in four Postgres databases (Docker container `shopfloor-pg`),
registered in a local Zetaris instance as `sf_fleet`, `sf_hr`, `sf_plant` and `sf_ops`. Models are served
through Meterless Nightshift. These rules apply to any coding assistant (Cursor, Codex, Claude Code).

## House rules
- **Local only.** Never push, add a git remote or use GitHub. Local commits are fine when the user asks.
- **Secrets.** Settings are in `.env`, which git ignores. Never print, log or commit its values, and never put
  them in prompts or URLs. To check a setting, test whether it is set without showing it.
- **Ask first** before installing packages, starting or removing containers, downloading data, or running
  anything that creates, drops or writes in Zetaris. Never run `docker compose down -v` or touch Docker
  volumes. Leave `~/hackathon/zetaris-platform`, `~/hackathon/getting-started` and
  `~/hackathon/open-agent-hack` unchanged.

## Preflight (read-only)
```bash
.venv/bin/python -m app.zetaris "SELECT 1"            # Zetaris REST answers [['1']]
.venv/bin/python -m app.zetaris "SHOW DATASOURCES"    # sf_fleet, sf_hr, sf_plant, sf_ops
docker ps --format '{{.Names}} {{.Status}}'          # shopfloor-pg and the zetaris-* containers are up
.venv/bin/python run.py models                        # Nightshift model ids (needs Nightshift running)
```
Report a short pass/fail list, and stop if anything fails.

## Querying data
- Use `app/zetaris.py`: `run_sql(sql)` in code, or `.venv/bin/python -m app.zetaris "<SQL>"` from the shell.
- Zetaris Lightning SQL, read-only first (`SHOW`, `DESCRIBE`, `SELECT`). Send one statement per call, with no
  trailing semicolon. Qualify tables as `<datasource>.<table>`, for example `sf_ops.incidents`.
  Joins across datasources work.
- Syntax reference: `~/hackathon/getting-started/docs/guides/zetaris-lightning-sql-commands.md`.
- `sf_ops.data_sources` lists which real source fed which table. `README.md` describes every table and
  the patterns planted in the data.

## Changing data
- The generator is `app/data/generate.py`. It uses only the Python standard library, uses seed 42, and reads
  the committed real-data summaries in `data/seed/`.
- Reload: `bash scripts/load_data.sh`, then `.venv/bin/python -m app.zetaris refresh`. This rebuilds the four
  databases.
- `app/data/fetch_sources.py` is an optional step that rebuilds `data/seed/` from the original sources. It
  needs network access, and the Bosch step needs `pip install -r requirements-data.txt`. Ask first.

## Running the app
- `.venv/bin/python run.py context --zone FINAL` runs one federated Zetaris query and asks Nemotron for a
  summary. Zone ids are in `sf_ops.zones`, for example FINAL, TRIM or X-WEST.
- Model lanes in `.env`: `NEMOTRON_MODEL` (auto), `NEMOTRON_DEEP`, `NEMOTRON_FAST`, `NEMOTRON_LOCAL`. Do not call
  the local lane without asking, because it can trigger a large model download.
