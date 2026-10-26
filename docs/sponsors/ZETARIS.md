# Zetaris in Shop-Floor OS

Part of the [sponsor technology docs](README.md). Back to the [README](../../README.md#zetaris-the-data-layer).

Every read the agents make goes through Zetaris ([`app/zetaris.py`](../../app/zetaris.py), the only data access layer), and every
Zetaris object (datasources, the read-only role, the Unified Semantic Layer and its data-quality rule) is created
in code through the Zetaris SQL REST API. This page follows Zetaris' evaluation criteria in order. All query results
below are real rows from the local Zetaris instance, run on 26 Oct 2026 through the agents' read-only login; the
full output is in [`zetaris/queries/RESULTS.md`](../../zetaris/queries/RESULTS.md).

## 1a. Use case and data sources
**Use case.** A car final-assembly hall stops when a kit is missing, an AGV stalls, coolant spills or a robot cell
faults. Before acting, the agents must answer, in seconds: who is qualified and on shift, which robot is healthy
and where it is, which safety rule applies, and whether this alarm is usually real. Those facts live in four
systems, and Zetaris federates them so that each incident starts from **one** query.

| Datasource | Postgres database | What it holds | Why the agents need it |
|---|---|---|---|
| `sf_fleet` | `fleet` | 8 AGVs (status, battery health, faults in 90 days, last service), 95,964 AGV tasks, telemetry with fault codes, hourly fleet load | The candidates agent sends only healthy robots; the dry run needs each AGV's live position |
| `sf_hr` | `hr` | 24 workers (role, shift, skills, station), 18 certificates with expiry dates, daily rosters | The critic and the gate block anyone without an in-date certificate (lockout/tagout, forklift, robot-cell entry) |
| `sf_plant` | `plant` | 10 stations and their zones, 8 machines, hourly machine telemetry (AI4I-shaped), Bosch CNC vibration recordings, production plan, units, work orders, weather | The monitor tells a real alarm from sensor noise; the World Model reads the layout |
| `sf_ops` | `ops` | 2,071 historical incidents, andon pulls, OSHA/SOP safety rules, floor grid, spill kits, Lab experiments, and the agents' own steps, model calls and journal | Cost of waiting vs acting, false-alarm rates, the rules the critic enforces, and the agents' history for *Ask the floor* |

Four sources, above the minimum of three. The data is synthetic, shaped by public data (see
[docs/data.md](../data.md)).

## 1b. Each source: onboarding, operability, exploration
**Onboarding, all four in code.** `python -m app.zetaris register` runs, for each database,
`CREATE DATASOURCE sf_<db> DESCRIBE BY "PostgreSQL" OPTIONS (...)` and `REGISTER DATASOURCE TABLES FROM sf_<db>`
([`app/zetaris.py`](../../app/zetaris.py) `register`). After a reload, `python -m app.zetaris refresh` runs `REGISTER`,
`REFRESH DATASOURCE TABLES` and `UNCACHE DATASOURCE` for each. The password comes from `.env` and never reaches a
log (`_scrub`). `SHOW DATASOURCES` lists `sf_fleet`, `sf_hr`, `sf_plant` and `sf_ops` as PostgreSQL datasources.

**Operable.** Every query below ran through Zetaris on the agents' read-only login, in 78 to 831 ms. Each source
has its own file of exploratory queries in [`zetaris/queries/`](../../zetaris/queries); rerun them all with
`.venv/bin/python scripts/zetaris_queries.py`, which rewrites `RESULTS.md`.

### sf_fleet ([`sf_fleet.sql`](../../zetaris/queries/sf_fleet.sql))
| Question | Result |
|---|---|
| Is the fleet operable, and how healthy is it? | 8 AGVs, all `idle` at the snapshot, mean battery health 86.6%, 200 faults in 90 days |
| Which AGVs abort or delay the most tasks? | AGV-3: 158 aborted of 13,227 tasks, almost 4 times the next (AGV-2, 43) |
| Which fault codes does telemetry report most? | `E52_NAV_LOST` 353 readings on 7 AGVs, then `E40_BATTERY_LOW` 53 |

### sf_hr ([`sf_hr.sql`](../../zetaris/queries/sf_hr.sql))
| Question | Result |
|---|---|
| Who works here, by role and shift? | 14 role-shift groups, for example 6 line operators on A and 6 on B, one maintenance technician per shift |
| Which certificates have lapsed on the plant date (16 Oct 2026)? | 1 of 3 forklift and 1 of 4 lockout/tagout certificates; first aid and robot-cell entry all in date |
| Who is rostered on the plant date? | Shift A: 10 on, 1 on leave. Shift B: 8 on, 1 absent, 1 on leave. Night: 1 on, 2 absent |

### sf_plant ([`sf_plant.sql`](../../zetaris/queries/sf_plant.sql))
| Question | Result |
|---|---|
| How is the line laid out? | 6 zones, 10 stations; FINAL has 3 stations, 2 kitted, and a station with no line-side buffer |
| Which machines fail most in telemetry? | M-04 (164 failures, 114 heat-dissipation) and M-03 (136, 90): the two machines whose overheats are real |
| How close does each shift get to plan? | Shift A builds 84.6% of plan with 4,558 min downtime; shift B 88.0% with 3,313 |

### sf_ops ([`sf_ops.sql`](../../zetaris/queries/sf_ops.sql))
| Question | Result |
|---|---|
| Which incident kinds cost the most? | Part shortages (821 incidents, 2,919 min downtime, 2,333 cars); 26% of machine overheats are false alarms |
| Which andon reasons stop the line? | Equipment stops the line in 473 of 585 pulls; missing parts in 314 of 1,006 |
| Which rules does the critic reject plans for most? (the agents' own steps) | R-CROSSING 604, R-TORQUE-QA 552, R-LOTO 241 |

The last query reads the agents' own history: `logs/` is synced every 30 s into `sf_ops.agent_steps`,
`sf_ops.llm_calls` and `sf_ops.journal` ([`app/logsync.py`](../../app/logsync.py)), so the agents can be questioned
about their behaviour with the same SQL as the plant.

## 1c. The Unified Semantic Layer
| | |
|---|---|
| Name | `floor` |
| Namespace | `lightning.metastore.shopfloor` (tables are `lightning.metastore.shopfloor.floor.<table>`) |
| Defined in | [`app/usl.py`](../../app/usl.py) `TABLES`: 9 business entities with 9 primary and 13 foreign keys |
| DDL | [`zetaris/usl/floor.ddl.sql`](../../zetaris/usl/floor.ddl.sql), printed by `python -m app.usl ddl`: one `COMPILE USL floor DEPLOY NAMESPACE ... DDL` and 9 `ACTIVATE USL TABLE ... AS SELECT ...` over the raw sources |
| Deployed by | `python -m app.usl deploy` (creates the namespace, compiles, activates each table, grants SELECT to the agents' role) |
| Used by | `run.py context` reads the zone's incidents, stations, machines, workers, certificates, AGVs and recordings through it, and falls back to the raw sources if it is unavailable ([`app/engines/shopfloor_world.py`](../../app/engines/shopfloor_world.py)); `logs/world_model.jsonl` records which path was used |

The entities: `zone`, `station` (→ zone), `machine` (→ station, zone), `worker` (→ station, home zone),
`certification` (→ worker), `agv`, `vibration_sample` (→ machine), `safety_rule`, and `incident` (→ zone, station,
machine, AGV, worker, vibration recording).

**The USL is active.** Queries from [`usl.sql`](../../zetaris/queries/usl.sql):

1. `SHOW NAMESPACES OR TABLES IN lightning.metastore.shopfloor.floor`: 9 tables, each with activation `true`.
2. Downtime by zone, following incident → station → zone over the declared keys:

   | zone | incidents | downtime_min |
   |---|---|---|
   | Final assembly | 789 | 2930 |
   | Body shop | 190 | 1651 |
   | Trim line | 363 | 1376 |
   | Marriage station | 226 | 914 |
   | Paint shop | 130 | 637 |
   | End-of-line test | 30 | 0 |

3. Bosch vibration label against the false-alarm flag, joining an `sf_ops` incident to an `sf_plant` recording in
   one query: every *normal* recording (20) belongs to a false alarm, and no *abnormal* one (130) does. This is the
   evidence the monitor uses to tell a real overheat from sensor noise.
4. Workers with a lapsed certificate (worker → certification): W-22 (maintenance technician, lockout/tagout,
   expired 29 Aug 2026) and W-21 (material handler, forklift, expired 14 Aug 2026).

`LIST DQ USL lightning.metastore.shopfloor.floor` returns the 22 key constraints Zetaris holds for the model
(9 primary, 13 foreign), plus the data-quality rule below.

### Data-quality rule
Zetaris evaluates data-quality rules, boolean expressions registered on an activated USL table
(`REGISTER DQ`, `SHOW DQ INVALID RECORD`; section 14 of the Lightning SQL reference). We use it for the rule the
safety critic depends on most:

```sql
REGISTER DQ cert_in_date TABLE lightning.metastore.shopfloor.floor.certification AS
  expires_date IS NOT NULL AND expires_date >= DATE '2026-10-16'
SHOW DQ INVALID RECORD cert_in_date TABLE lightning.metastore.shopfloor.floor.certification
```

"Every certificate on file is in date on the plant date." A record that fails is a person the candidates agent
must not send into work that needs that certificate. The plant data is dated 16 Oct 2026, so the rule uses that
date rather than the wall clock. Code: [`app/usl.py`](../../app/usl.py) `DQ_RULES` and `dq()`, run with
`.venv/bin/python -m app.usl dq`; offline test in [`tests/test_usl.py`](../../tests/test_usl.py).

**Result (26 Oct 2026).** The rule was registered with `python -m app.usl dq` (an admin write, run by the team),
then read back on the agents' read-only login ([`dq.sql`](../../zetaris/queries/dq.sql), output in
[`RESULTS.md`](../../zetaris/queries/RESULTS.md)):

- `LIST DQ USL lightning.metastore.shopfloor.floor` now returns 23 rows: the 22 key constraints and
  `cert_in_date | certification | Custom Data Quality | expires_date IS NOT NULL AND expires_date >= DATE '2026-10-16'`.
- `SHOW DQ INVALID RECORD cert_in_date ...` returns the 2 failing records out of 18:

  | cert_id | worker_id | cert | issued_date | expires_date |
  |---|---|---|---|---|
  | W-21:forklift | W-21 | forklift | 2023-08-15 | 2026-08-14 |
  | W-22:lockout_tagout | W-22 | lockout_tagout | 2025-08-29 | 2026-08-29 |

These are the two people the critic would block from forklift work and from lockout/tagout repairs, and Zetaris
finds them without our code. The same two show up in the USL join query above.

## 2a. Time saved by the USL compared with hand-coding
Counted from this repository. Where a number is an estimate, it says so.

| Concern | With Zetaris and the USL (this repo) | Hand-coded against four Postgres databases |
|---|---|---|
| Connectors | 4 `CREATE DATASOURCE` statements generated by one 20-line function (`register`), no database driver in `requirements.txt` | A Postgres driver as a new dependency, 4 connection configs, and pooling and retries per database |
| Cross-source joins | One SQL statement. `PLANT_SQL` ([`plant_world.py`](../../app/engines/plant_world.py)) is a 7-part `UNION ALL` over all 4 databases; `ZONE_SQL` ([`shopfloor_world.py`](../../app/engines/shopfloor_world.py)) joins 7 tables from 4 databases with 6 joins | Postgres cannot join across databases without an extension, so each of these becomes 4 queries plus a join in Python. That is our estimate of 40 to 60 lines of Python for each of these two reads |
| Keys | Declared once: 9 primary and 13 foreign keys, which Zetaris lists back (`LIST DQ USL`) | Join conditions repeated in every query that needs them (26 distinct `sf_*` tables are referenced in `app/`) |
| Access control | The read-only role is 9 statements (`reader_role_sql`: 1 `CREATE ROLE`, 4 `ASSIGN`, 4 `GRANT`) plus 1 USL grant; Zetaris refuses an agent's write itself | A Postgres role per database, with grants on each table in 4 databases, and a second set of credentials in the app |
| Read-only guard | Still ours: [`sqlguard.py`](../../app/investigate/sqlguard.py) (63 lines) checks one statement, a whitelist and a `LIMIT` before Zetaris sees it | The same 63 lines |
| Data quality | One `REGISTER DQ` statement; Zetaris returns the failing records | A scheduled SQL check and somewhere to store its results |

**What it did not save.** The USL activations are plain `SELECT`s over the sources, and `zone_query(semantic=True)`
only swaps table names, so the query text the agents send is no shorter through the USL than through the raw
sources. The saving is in what is declared once (keys, grants, rules) and in not writing a join layer at all.

**Not done:** a runnable hand-coded version for a side-by-side count. It would need a Postgres driver, which
the project rules say to ask about before adding, so the "hand-coded" column above is reasoned, not measured.

## 2b. Would the USL help in real work?
**Where it helps.** Plants run MES, CMMS, fleet management, HR and learning systems on different databases, and
every analytics, safety or AI project rewrites the same joins (asset to work order to technician to
certificate). A USL holds those keys once, gives every consumer the same names, and gives an AI agent one
read-only surface with its own role. That is what our agents needed.

**Where it does not.** It is not for the control loop: the reflex fences an area in under 100 ms in code, and no
federated query belongs in that path. It does not replace a write path (our Lab writes one row back), and the
data is only as fresh as the sources it federates.

**A project that would benefit, with a forecast.** A plant-wide maintenance-readiness report that joins CMMS work
orders, technician certificates (HR or LMS), machine telemetry and production losses (MES) in three plants.
Assumptions, to be checked against a real site: 3 plants × 4 sources = 12 connections; about 15 recurring
cross-source joins; 2 consumer teams. Hand-coded, we would expect roughly 1 to 2 developer-days per source for
connection and access set-up and about half a day per join to write and test, about 20 to 30 days in all. With
Zetaris, connections and the read-only role are a few statements each, and the joins are declared once in a USL
that both teams query: we estimate 6 to 10 days, mostly spent modelling the keys. This is a forecast from our
own build, not a measurement.

## 2c. A killer requirement for Zetaris
**Problem.** USL access control can only be granted in the GUI (Management → Permissions, then Data Product →
Unified Semantic Layer → Access Control), and a redeploy of the USL empties those lists. In our build,
everything else was code: datasources, the read-only role and the USL itself. After each `COMPILE USL ... DEPLOY`,
the agents' role silently lost access, and the agents fell back to the raw sources until someone clicked through
the GUI again (README *Setup*, step 4).

**User.** A platform engineer who deploys an agent's data layer from CI, and the AI agents that read through a
least-privilege role.

**Requirement.** USL access control is fully scriptable in Lightning SQL and survives a redeploy.

**Acceptance criteria.**
1. `GRANT SELECT ON USL <namespace.usl> TO ROLE '<role>'` (and on a single USL table) gives the role read access
   with no GUI step, and `REVOKE` removes it.
2. `SHOW GRANTS ON USL <namespace.usl>` lists the roles and their privileges.
3. `COMPILE USL <usl> DEPLOY ...` keeps existing grants for tables that still exist, and reports any it dropped.
4. A role without the grant gets a clear permission error, not an empty result.
5. Every grant and revoke appears in an audit log with the user who made it.

**Mock DDL.**
```sql
GRANT SELECT ON USL lightning.metastore.shopfloor.floor TO ROLE 'sf_agent_reader'
GRANT SELECT ON USL TABLE lightning.metastore.shopfloor.floor.incident TO ROLE 'sf_analyst'
SHOW GRANTS ON USL lightning.metastore.shopfloor.floor
COMPILE USL floor DEPLOY NAMESPACE lightning.metastore.shopfloor KEEP GRANTS DDL ...
```

A second, smaller request from the same build: a data-quality rule is a boolean expression on one table, so
"every worker assigned to robot-cell work holds an in-date robot-cell certificate" (a join across worker and
certification) has to stay a SQL check. We did not test whether a subquery is accepted inside `REGISTER DQ`.

## More Zetaris in this build
- **Read-only agents.** The agents read on their own Zetaris login whose only role, `sf_agent_reader`, has SELECT
  on the four datasources and the USL (`python -m app.zetaris reader-role`). A write from an agent is refused by
  Zetaris itself, on top of our SQL guard (one read-only statement, whitelisted `sf_*` tables, capped `LIMIT`).
  The operator login is kept for register, refresh and the Lab's write.
- **Investigations.** *Ask the floor*'s SQL agent writes Spark SQL from the table catalogue
  ([`app/investigate/catalog.py`](../../app/investigate/catalog.py)) and runs it on Zetaris; a refused or failed query goes back to the
  SQL agent with the reason for one retry.
- **Writes back.** Lab proposals go back through Zetaris (`INSERT INTO sf_ops.experiments`).
- **The agents' own logs in Zetaris.** Agent steps, model calls and the hash-chained journal are synced into
  `sf_ops.agent_steps`, `sf_ops.llm_calls` and `sf_ops.journal`, so "how often did the critic reject the planner,
  and why?" is one SQL query (shown in 1b).
