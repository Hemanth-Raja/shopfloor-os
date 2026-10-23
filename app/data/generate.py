"""Generate the Shop-Floor OS datasets as SQL files, one per source database.

Standard library only. Deterministic (seed 42), so every run gives the same data.

    python -m app.data.generate            # writes data/sql/{fleet,hr,plant,ops}.sql

Sources:
  - Synthetic (ours): floor map, zones, AGVs, workers, shifts, incident history.
  - UCI AI4I 2020 Predictive Maintenance (CC BY 4.0, DOI 10.24432/C5HS5C):
    used for machine telemetry when data/raw/ai4i2020.csv exists, otherwise
    telemetry is synthesised with the same columns.
  - OSHA 29 CFR 1910.178 and 1910.22 (U.S. Department of Labor, public domain):
    paraphrased rule summaries in SAFETY_RULES; verify wording against eCFR.
"""

import csv
import random
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "data" / "raw"
OUT = ROOT / "data" / "sql"
rng = random.Random(42)

ROWS, COLS = 10, 20
START = datetime(2026, 7, 1, 6, 0)

# zone_id, name, kind, x0, y0, x1, y1 (inclusive grid cells; x = column, y = row)
ZONES = [
    ("DOCK", "Loading dock", "dock", 0, 0, 3, 9),
    ("A", "Aisle A", "aisle", 5, 1, 18, 1),
    ("B", "Aisle B", "aisle", 5, 3, 18, 3),
    ("C", "Aisle C", "aisle", 5, 5, 18, 5),
    ("D", "Aisle D", "aisle", 5, 7, 18, 7),
    ("MAIN", "Main walkway", "walkway", 4, 0, 4, 9),
    ("BAY", "Machine bay", "machines", 15, 8, 19, 9),
    ("CHG", "Charging station", "charger", 19, 0, 19, 1),
]
# Shelving occupies rows 2, 4, 6 between columns 5 and 18 (not walkable).
SHELVES = [(x, y) for y in (2, 4, 6) for x in range(5, 19)]

AGVS = [
    ("AGV-1", "Tugger T200", 1, 1, 500),
    ("AGV-2", "Tugger T200", 1, 4, 500),
    ("AGV-3", "Lifter L800", 2, 6, 800),
    ("AGV-4", "Lifter L800", 2, 8, 800),
]

# All names are invented for the simulation.
WORKERS = [
    ("W-01", "Sarah Okafor", "maintenance_tech", "spill_cleanup,forklift,first_aid,agv_reset", True, "DOCK"),
    ("W-02", "Tom Becker", "picker", "spill_cleanup,manual_handling", False, "A"),
    ("W-03", "Priya Nair", "maintenance_tech", "machine_repair,agv_reset,lockout_tagout", True, "BAY"),
    ("W-04", "Luis Romero", "forklift_operator", "forklift,pallet_recovery", True, "DOCK"),
    ("W-05", "Mei Chen", "picker", "manual_handling,first_aid", False, "C"),
    ("W-06", "Jonas Weber", "shift_supervisor", "first_aid,lockout_tagout,incident_command", True, "MAIN"),
]
WORKER_POS = {"W-01": (2, 2), "W-02": (8, 1), "W-03": (16, 9), "W-04": (1, 7), "W-05": (10, 5), "W-06": (4, 5)}

MACHINES = [
    ("M-1", "Stretch wrapper", "wrapper", 15, 9, "BAY", "L"),
    ("M-2", "Conveyor sorter", "conveyor", 17, 8, "BAY", "M"),
    ("M-3", "Box erector", "packer", 19, 9, "BAY", "H"),
]

# rule_id, cfr_ref, summary (paraphrased), check, params
SAFETY_RULES = [
    ("R-TRAIN", "29 CFR 1910.178(l)", "Only trained and evaluated operators may operate a powered industrial truck.",
     "requires_skill", "skill=forklift;applies_to=pallet_recovery"),
    ("R-PED-CLEAR", "29 CFR 1910.178(m)(1)", "Do not drive a truck toward a person standing in front of a fixed object.",
     "min_separation", "cells=2"),
    ("R-UNDER-LOAD", "29 CFR 1910.178(m)(3)", "No person may stand or pass under the elevated part of a truck.",
     "no_shared_cell", "agv_model=Lifter L800"),
    ("R-CROSS-AISLE", "29 CFR 1910.178(n)(4)", "Slow down and sound the horn at cross aisles and where vision is obstructed.",
     "max_speed_at", "zone=MAIN;speed_mps=0.5"),
    ("R-SAFE-SPEED", "29 CFR 1910.178(n)(8)", "Operate at a speed that allows a safe stop under the conditions.",
     "max_speed", "speed_mps=1.5"),
    ("R-SURFACE", "29 CFR 1910.22(a)(1)-(3)", "Keep walking-working surfaces clean, dry and free of hazards such as spills.",
     "zone_blocked_until_clear", "types=spill,obstruction"),
    ("R-LOTO", "29 CFR 1910.147", "Repair of energised machinery needs lockout/tagout by an authorised person.",
     "requires_skill", "skill=lockout_tagout;applies_to=machine_repair"),
]

INCIDENT_TYPES = ["spill", "obstruction", "agv_fault", "machine_overheat"]
# Aisle B is a hot spot, so the lab has something to find.
SPILL_ZONE_WEIGHTS = {"A": 2, "B": 6, "C": 2, "D": 1, "DOCK": 2, "MAIN": 1}


def q(v):
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, datetime):
        return f"'{v.isoformat(sep=' ')}'"
    return "'" + str(v).replace("'", "''") + "'"


def inserts(table, cols, rows, batch=500):
    out = []
    for i in range(0, len(rows), batch):
        values = ",\n".join("(" + ",".join(q(v) for v in r) + ")" for r in rows[i:i + batch])
        out.append(f"INSERT INTO {table} ({','.join(cols)}) VALUES\n{values};")
    return "\n".join(out)


def walkable(x, y):
    return 0 <= x < COLS and 0 <= y < ROWS and (x, y) not in SHELVES


def random_cell_in(zone_id):
    z = next(z for z in ZONES if z[0] == zone_id)
    cells = [(x, y) for x in range(z[3], z[5] + 1) for y in range(z[4], z[6] + 1) if walkable(x, y)]
    return rng.choice(cells)


def fleet_sql():
    agvs = [(a, m, x, y, rng.randint(55, 98), "idle", p, (START - timedelta(days=rng.randint(10, 120))).date().isoformat(),
             rng.choice([0, 1, 1, 2, 5]) if a == "AGV-3" else rng.choice([0, 0, 1]))
            for a, m, x, y, p in AGVS]
    telemetry = []
    for a, *_ in AGVS:
        x, y, batt = next((r[2], r[3], r[4]) for r in agvs if r[0] == a)
        t = START
        for _ in range(720):  # 12 hours at 1 sample a minute
            nx, ny = x + rng.choice([-1, 0, 1]), y + rng.choice([-1, 0, 1])
            if walkable(nx, ny):
                x, y = nx, ny
            batt = max(15, batt - rng.random() * 0.15)
            fault = "E12_WHEEL_SLIP" if a == "AGV-3" and rng.random() < 0.01 else None
            telemetry.append((t, a, x, y, round(rng.uniform(0, 1.4), 2), round(batt, 1), fault))
            t += timedelta(minutes=1)
    return "\n".join([
        "CREATE TABLE agvs (agv_id TEXT PRIMARY KEY, model TEXT, home_x INT, home_y INT, battery_pct INT, status TEXT,"
        " max_payload_kg INT, last_service_date DATE, faults_90d INT);",
        inserts("agvs", ["agv_id", "model", "home_x", "home_y", "battery_pct", "status", "max_payload_kg",
                         "last_service_date", "faults_90d"], agvs),
        "CREATE TABLE agv_telemetry (ts TIMESTAMP, agv_id TEXT, x INT, y INT, speed_mps REAL, battery_pct REAL, fault_code TEXT);",
        inserts("agv_telemetry", ["ts", "agv_id", "x", "y", "speed_mps", "battery_pct", "fault_code"], telemetry),
    ])


def hr_sql():
    workers = [(w, n, r, s, f, z, WORKER_POS[w][0], WORKER_POS[w][1]) for w, n, r, s, f, z in WORKERS]
    shifts = []
    for d in range(14):
        day = (START + timedelta(days=d)).date().isoformat()
        for w, *_ in WORKERS:
            if rng.random() < 0.85:
                shifts.append((w, day, "06:00", "14:00", "on" if rng.random() < 0.9 else "break"))
    return "\n".join([
        "CREATE TABLE workers (worker_id TEXT PRIMARY KEY, name TEXT, role TEXT, skills TEXT, forklift_licence BOOLEAN,"
        " home_zone TEXT, x INT, y INT);",
        inserts("workers", ["worker_id", "name", "role", "skills", "forklift_licence", "home_zone", "x", "y"], workers),
        "CREATE TABLE shifts (worker_id TEXT, shift_date DATE, start_time TEXT, end_time TEXT, status TEXT);",
        inserts("shifts", ["worker_id", "shift_date", "start_time", "end_time", "status"], shifts),
    ])


def ai4i_rows():
    path = RAW / "ai4i2020.csv"
    if path.exists():
        with path.open(newline="") as f:
            for r in csv.DictReader(f):
                yield (float(r["Air temperature [K]"]), float(r["Process temperature [K]"]),
                       int(r["Rotational speed [rpm]"]), float(r["Torque [Nm]"]), int(r["Tool wear [min]"]),
                       int(r["Machine failure"]), int(r["HDF"]), int(r["OSF"]), r["Type"], "ai4i2020")
        return
    print("data/raw/ai4i2020.csv not found: synthesising machine telemetry instead")
    for _ in range(3000):
        air = rng.gauss(300, 2)
        proc = air + rng.gauss(10, 1)
        rpm = int(rng.gauss(1540, 180))
        torque = max(3.0, rng.gauss(40, 10))
        wear = rng.randint(0, 250)
        hdf = int(proc - air < 8.6 and rpm < 1380)
        osf = int(wear * torque > 11000)
        yield (round(air, 1), round(proc, 1), rpm, round(torque, 1), wear, int(hdf or osf), hdf, osf,
               rng.choice("LMH"), "synthetic")


def plant_sql():
    rows = list(ai4i_rows())
    telemetry = []
    for m_id, *_rest, ptype in MACHINES:
        pool = [r for r in rows if r[8] == ptype] or rows
        t = START
        for r in pool[:1000]:
            telemetry.append((t, m_id, *r[:8], r[9]))
            t += timedelta(minutes=1)
    return "\n".join([
        "CREATE TABLE machines (machine_id TEXT PRIMARY KEY, name TEXT, kind TEXT, x INT, y INT, zone_id TEXT, product_type TEXT);",
        inserts("machines", ["machine_id", "name", "kind", "x", "y", "zone_id", "product_type"], MACHINES),
        "CREATE TABLE machine_telemetry (ts TIMESTAMP, machine_id TEXT, air_temp_k REAL, process_temp_k REAL,"
        " rpm INT, torque_nm REAL, tool_wear_min INT, failure INT, heat_dissipation_failure INT,"
        " overstrain_failure INT, source TEXT);",
        inserts("machine_telemetry", ["ts", "machine_id", "air_temp_k", "process_temp_k", "rpm", "torque_nm",
                                      "tool_wear_min", "failure", "heat_dissipation_failure", "overstrain_failure",
                                      "source"], telemetry),
    ])


def incident_history(n=200):
    """Past incidents with outcomes. Time to clear depends on distance to a spill kit,
    responder skill and false alarms, so the lab can test policies against it."""
    kit_cell = (1, 0)  # today all spill kits are kept at the dock
    rows = []
    for i in range(n):
        ts = START + timedelta(hours=rng.randint(0, 24 * 60))
        kind = rng.choices(INCIDENT_TYPES, weights=[5, 3, 2, 2])[0]
        if kind in ("spill", "obstruction"):
            zone = rng.choices(list(SPILL_ZONE_WEIGHTS), weights=list(SPILL_ZONE_WEIGHTS.values()))[0]
        elif kind == "machine_overheat":
            zone = "BAY"
        else:
            zone = rng.choice(["A", "B", "C", "D", "MAIN", "CHG"])
        x, y = random_cell_in(zone)
        false_alarm = rng.random() < (0.25 if kind == "machine_overheat" else 0.08)
        severity = rng.choices(["low", "medium", "high"], weights=[4, 4, 2])[0]
        worker = {"spill": "W-01", "obstruction": "W-04", "agv_fault": "W-03", "machine_overheat": "W-03"}[kind]
        if rng.random() < 0.3:
            worker = rng.choice([w[0] for w in WORKERS])
        agv = rng.choice([a[0] for a in AGVS]) if kind != "machine_overheat" else None
        kit_dist = abs(x - kit_cell[0]) + abs(y - kit_cell[1])
        base = {"spill": 240, "obstruction": 300, "agv_fault": 420, "machine_overheat": 360}[kind]
        clear_s = 0 if false_alarm else int(base + kit_dist * 9 * (kind == "spill") + rng.gauss(0, 45)
                                            + (180 if worker not in ("W-01", "W-03", "W-04") else 0))
        line_stop_s = int(clear_s * (0.9 if severity == "high" else 0.4)) if not false_alarm else rng.choice([0, 0, 300])
        outcome = "false_alarm" if false_alarm else rng.choices(["resolved", "resolved_with_rework"], weights=[9, 1])[0]
        rows.append((f"INC-{i + 1:04d}", ts, kind, zone, x, y, severity, agv, worker, max(clear_s, 0),
                     line_stop_s, outcome, false_alarm, kit_dist))
    return sorted(rows, key=lambda r: r[1])


def ops_sql():
    grid = [(x, y, "shelf" if (x, y) in SHELVES else next((z[0] for z in ZONES if z[3] <= x <= z[5] and z[4] <= y <= z[6]), "FLOOR"))
            for y in range(ROWS) for x in range(COLS)]
    return "\n".join([
        "CREATE TABLE zones (zone_id TEXT PRIMARY KEY, name TEXT, kind TEXT, x0 INT, y0 INT, x1 INT, y1 INT);",
        inserts("zones", ["zone_id", "name", "kind", "x0", "y0", "x1", "y1"], ZONES),
        "CREATE TABLE floor_cells (x INT, y INT, cell TEXT);",
        inserts("floor_cells", ["x", "y", "cell"], grid),
        "CREATE TABLE safety_rules (rule_id TEXT PRIMARY KEY, cfr_ref TEXT, summary TEXT, check_type TEXT, params TEXT);",
        inserts("safety_rules", ["rule_id", "cfr_ref", "summary", "check_type", "params"], SAFETY_RULES),
        "CREATE TABLE incidents (incident_id TEXT PRIMARY KEY, ts TIMESTAMP, kind TEXT, zone_id TEXT, x INT, y INT,"
        " severity TEXT, agv_id TEXT, worker_id TEXT, time_to_clear_s INT, line_stop_s INT, outcome TEXT,"
        " false_alarm BOOLEAN, kit_distance INT);",
        inserts("incidents", ["incident_id", "ts", "kind", "zone_id", "x", "y", "severity", "agv_id", "worker_id",
                              "time_to_clear_s", "line_stop_s", "outcome", "false_alarm", "kit_distance"],
                incident_history()),
        "CREATE TABLE experiments (experiment_id TEXT PRIMARY KEY, created_at TIMESTAMP, hypothesis TEXT, policy TEXT,"
        " baseline_mean_s REAL, treatment_mean_s REAL, effect_pct REAL, ci_low REAL, ci_high REAL, status TEXT);",
    ])


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for name, fn in [("fleet", fleet_sql), ("hr", hr_sql), ("plant", plant_sql), ("ops", ops_sql)]:
        path = OUT / f"{name}.sql"
        path.write_text(fn() + "\n")
        print(f"wrote {path.relative_to(ROOT)} ({path.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
