"""Zetaris Unified Semantic Layer for the shop floor: one semantic model over the four sf_* sources.

The USL `floor` (namespace lightning.metastore.shopfloor) names the business entities once, with primary and
foreign keys: zone, station, machine, worker, certification, agv, vibration_sample, safety_rule and incident.
Each table is activated with a SELECT over the raw sources, so it always reads live data; the joins the agents
need (incident -> station, machine, worker, agv, recording) follow the declared keys instead of being rewritten in
every query. Query a table as lightning.metastore.shopfloor.floor.<table>.

    .venv/bin/python -m app.usl compile    # check the model, store nothing (admin login)
    .venv/bin/python -m app.usl deploy     # create namespace, deploy, activate, grant SELECT to the agents' role
    .venv/bin/python -m app.usl status     # tables and activation flags (agents' login)
    .venv/bin/python -m app.usl dq         # register the data-quality rules, show the records that fail them
    .venv/bin/python -m app.usl ddl        # print the COMPILE and ACTIVATE statements (no Zetaris call)
"""

from __future__ import annotations

import sys

from app.zetaris import READER_ROLE, run_sql

NAMESPACE = "lightning.metastore.shopfloor"
USL = "floor"
PREFIX = f"{NAMESPACE}.{USL}"

# name -> (DDL body, activation SELECT). Columns must match the DDL's order and types.
TABLES: dict[str, tuple[str, str]] = {
    "zone": ("""zone_id varchar(20) NOT NULL PRIMARY KEY,
  name varchar(80),
  kind varchar(40)""",
             "SELECT COALESCE(zone_id, '') AS zone_id, name, kind FROM sf_ops.zones"),
    "station": ("""station_id varchar(20) NOT NULL PRIMARY KEY,
  name varchar(80),
  zone_id varchar(20) FOREIGN KEY REFERENCES zone(zone_id),
  x int,
  y int,
  kitted boolean,
  buffer_min int""",
                "SELECT COALESCE(station_id, '') AS station_id, name, zone_id, x, y, kitted, buffer_min "
                "FROM sf_plant.stations"),
    "machine": ("""machine_id varchar(20) NOT NULL PRIMARY KEY,
  name varchar(80),
  kind varchar(40),
  station_id varchar(20) FOREIGN KEY REFERENCES station(station_id),
  zone_id varchar(20) FOREIGN KEY REFERENCES zone(zone_id),
  commissioned_year int""",
                "SELECT COALESCE(machine_id, '') AS machine_id, name, kind, station_id, zone_id, commissioned_year "
                "FROM sf_plant.machines"),
    "worker": ("""worker_id varchar(20) NOT NULL PRIMARY KEY,
  name varchar(80),
  role varchar(40),
  shift_code varchar(4),
  station_id varchar(20) FOREIGN KEY REFERENCES station(station_id),
  home_zone varchar(20) FOREIGN KEY REFERENCES zone(zone_id),
  skills varchar(200),
  forklift_licence boolean""",
               "SELECT COALESCE(worker_id, '') AS worker_id, name, role, shift_code, station_id, home_zone, skills, "
               "forklift_licence FROM sf_hr.workers"),
    "certification": ("""cert_id varchar(80) NOT NULL PRIMARY KEY,
  worker_id varchar(20) FOREIGN KEY REFERENCES worker(worker_id),
  cert varchar(40),
  issued_date date,
  expires_date date""",
                      "SELECT COALESCE(concat(worker_id, ':', cert), '') AS cert_id, worker_id, cert, issued_date, "
                      "expires_date FROM sf_hr.certifications"),
    "agv": ("""agv_id varchar(20) NOT NULL PRIMARY KEY,
  model varchar(80),
  kind varchar(40),
  status varchar(20),
  battery_pct int,
  battery_health_pct int,
  faults_90d int,
  last_service_date date""",
            "SELECT COALESCE(agv_id, '') AS agv_id, model, kind, status, battery_pct, battery_health_pct, faults_90d, "
            "last_service_date FROM sf_fleet.agvs"),
    "vibration_sample": ("""recording varchar(80) NOT NULL PRIMARY KEY,
  bosch_machine varchar(20),
  label varchar(20),
  x_rms float,
  linked_machine_id varchar(20) FOREIGN KEY REFERENCES machine(machine_id)""",
                         "SELECT COALESCE(recording, '') AS recording, bosch_machine, label, x_rms, linked_machine_id "
                         "FROM sf_plant.vibration_samples"),
    "safety_rule": ("""rule_id varchar(40) NOT NULL PRIMARY KEY,
  cfr_ref varchar(80),
  check_type varchar(40),
  summary varchar(400)""",
                    "SELECT COALESCE(rule_id, '') AS rule_id, cfr_ref, check_type, summary FROM sf_ops.safety_rules"),
    "incident": ("""incident_id varchar(40) NOT NULL PRIMARY KEY,
  ts timestamp,
  shift_code varchar(4),
  kind varchar(40),
  cause varchar(200),
  zone_id varchar(20) FOREIGN KEY REFERENCES zone(zone_id),
  station_id varchar(20) FOREIGN KEY REFERENCES station(station_id),
  machine_id varchar(20) FOREIGN KEY REFERENCES machine(machine_id),
  agv_id varchar(20) FOREIGN KEY REFERENCES agv(agv_id),
  worker_id varchar(20) FOREIGN KEY REFERENCES worker(worker_id),
  vibration_recording varchar(80) FOREIGN KEY REFERENCES vibration_sample(recording),
  severity varchar(20),
  time_to_clear_s int,
  downtime_min float,
  cars_lost int,
  cost_usd int,
  outcome varchar(40),
  false_alarm boolean,
  description varchar(400)""",
                 "SELECT COALESCE(incident_id, '') AS incident_id, ts, shift_code, kind, cause, zone_id, station_id, "
                 "machine_id, agv_id, worker_id, vibration_recording, severity, time_to_clear_s, downtime_min, cars_lost, "
                 "cost_usd, outcome, false_alarm, description FROM sf_ops.incidents"),
}


def compile_sql(deploy: bool) -> str:
    """One COMPILE statement for the whole model; each CREATE TABLE starts on its own line."""
    ddl = ";\n".join(f"CREATE TABLE {name} (\n  {body}\n)" for name, (body, _) in TABLES.items())
    return f"COMPILE USL {USL}{' DEPLOY' if deploy else ''} NAMESPACE {NAMESPACE} DDL\n{ddl}"


def activate_sql(name: str) -> str:
    return f"ACTIVATE USL TABLE {PREFIX}.{name} AS {TABLES[name][1]}"


def deploy(run=run_sql, log=print) -> None:
    run("CREATE NAMESPACE IF NOT EXISTS lightning.metastore", write=True)  # a fresh Zetaris has no parent either
    run(f"CREATE NAMESPACE IF NOT EXISTS {NAMESPACE}", write=True)
    run(compile_sql(True), write=True)
    log(f"deployed {PREFIX} ({len(TABLES)} tables)")
    for name in TABLES:
        run(activate_sql(name), write=True)
        log(f"activated {PREFIX}.{name}")
    try:
        run(f"GRANT SELECT ON USL NAMESPACE {NAMESPACE} TO ROLE '{READER_ROLE}'", write=True)
        log(f"granted SELECT on {NAMESPACE} to {READER_ROLE}")
    except RuntimeError as err:  # re-running: the grant is still there
        if "already" not in str(err).lower():
            raise


def status(run=run_sql) -> list:
    return run(f"SHOW NAMESPACES OR TABLES IN {NAMESPACE}.{USL}")


# Zetaris data-quality rules: name -> (USL table, boolean expression a valid record satisfies). The plant's data is
# dated 16 Oct 2026 (the World Model's `today`), so "in date" is checked against that day, not the wall clock.
DQ_RULES: dict[str, tuple[str, str]] = {
    "cert_in_date": ("certification", "expires_date IS NOT NULL AND expires_date >= DATE '2026-10-16'"),
}


def dq_sql() -> list[str]:
    return [f"REGISTER DQ {name} TABLE {PREFIX}.{table} AS {expr}" for name, (table, expr) in DQ_RULES.items()]


def dq(run=run_sql, log=print) -> dict:
    """Register the rules (operator login; re-running keeps an existing rule), then list them and show the records
    that fail each one (agents' login)."""
    for sql in dq_sql():
        try:
            run(sql, write=True)
            log(sql)
        except RuntimeError as err:
            if "exist" not in str(err).lower():
                raise
            log(f"{sql}  (already registered)")
    out = {"rules": run(f"LIST DQ USL {PREFIX}"), "invalid": {}}
    for name, (table, _) in DQ_RULES.items():
        out["invalid"][name] = run(f"SHOW DQ INVALID RECORD {name} TABLE {PREFIX}.{table}")
    return out


if __name__ == "__main__":
    cmd = sys.argv[1:] or ["status"]
    if cmd == ["compile"]:
        print(str(run_sql(compile_sql(False), write=True))[:2000])
    elif cmd == ["deploy"]:
        deploy()
    elif cmd == ["ddl"]:
        print(compile_sql(True))
        for name in TABLES:
            print(f"\n{activate_sql(name)}")
    elif cmd == ["dq"]:
        res = dq()
        print("LIST DQ USL:")
        for row in res["rules"]:
            print(" ", row)
        for name, rows in res["invalid"].items():
            print(f"{name}: {len(rows)} invalid record(s)")
            for row in rows:
                print(" ", row)
    else:
        for row in status():
            print(row)
