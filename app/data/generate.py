"""Generate the Shop-Floor OS datasets as SQL files, one per source database.

The site is a fictional car final-assembly hall on a 20 x 40 grid: a body shop weld cell and
hemming press, a paint booth, trim, powertrain marriage, final assembly and end-of-line test,
fed by eight AGVs from a parts supermarket next to the receiving dock. 90 days of history with
two production shifts (A 06-14, B 14-22), a night maintenance shift (N 22-06) and some Saturday
overtime.

Standard library only. Deterministic (seed 42), so every run gives the same data.

    python -m app.data.generate            # writes data/sql/{fleet,hr,plant,ops}.sql from data/seed/
    python -m app.data.fetch_sources       # optional: rebuild data/seed/ from the original sources

Real-world anchors, read from the committed summaries in data/seed/ (each optional; a documented
fallback is used when a file is missing):
  - UCI AI4I 2020 Predictive Maintenance (CC BY 4.0, DOI 10.24432/C5HS5C): hourly machine telemetry.
  - Bosch CNC Machining dataset (CC BY 4.0): real vibration recordings, labelled normal/abnormal, that
    back the wheel nutrunner's torque alarms (real fault or sensor noise).
  - OSHA Severe Injury Reports, NAICS 3361 (public domain): the mix of hazard types among safety events.
  - BLS SOII, NAICS 3361 (public domain): the recordable injury rate that sets how many safety events occur.
  - Open-Meteo historical weather for Detroit, MI (CC BY 4.0, "Weather data by Open-Meteo.com"):
    outdoor temperature, which heats the machines and drives overheat alarms.
  - OSHA 29 CFR 1910.22, .147, .178, .212, .217 (public domain): paraphrased rule summaries.

Patterns planted for the agents to find (all synthetic):
  - F10 Wheels and tyres is the hot spot: most shortages, torque faults and defects.
  - AGV faults concentrate on AGV-3, an old kitting tugger overdue for service.
  - Shortages rise when the kitting fleet is busy (high demand or an AGV down).
  - Time to clear grows with distance from the supermarket (shortages) and spill kits (spills).
  - Machine overheats are more likely on hot days; about 25% are false alarms from noisy sensors.
  - The night shift is thinly staffed, so incidents clear more slowly.
  - Some repairs were done by a technician whose lockout/tagout certification had lapsed.
"""

import csv
import math
import random
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "data" / "raw"    # optional downloads, git-ignored
SEED = ROOT / "data" / "seed"  # small real-data summaries, committed; see app/data/fetch_sources.py
OUT = ROOT / "data" / "sql"
rng = random.Random(42)

ROWS, COLS = 20, 40            # grid cells; one cell is about 2 m
CELL_M = 2.0
START = datetime(2026, 7, 20)  # a Monday
DAYS = 90
END = START + timedelta(days=DAYS)
TELEMETRY_DAYS = 7             # AGV telemetry every minute for the last week
TAKT_S = 75                    # one car every 75 s
CARS_PER_HOUR = 3600 // TAKT_S
SHIFTS = {"A": (6, 14), "B": (14, 22), "N": (22, 30)}  # N ends 06:00 next day
PRODUCTION_SHIFTS = ("A", "B")

# Cost assumptions (ours, for ranking incidents; not industry figures)
LINE_STOP_USD_PER_MIN = 1500
LABOUR_USD_PER_HOUR = 85
REWORK_USD = 400

# zone_id, name, kind, x0, y0, x1, y1 (inclusive grid cells; x = column, y = row).
# First match wins, so the pedestrian crossings come before the lanes they cross.
ZONES = [
    ("X-WEST", "West pedestrian crossing", "crossing", 8, 6, 9, 15),
    ("X-EAST", "East pedestrian crossing", "crossing", 34, 6, 35, 15),
    ("RECV", "Receiving dock", "dock", 0, 0, 7, 5),
    ("BODY", "Body shop", "body_shop", 10, 0, 21, 5),
    ("PAINT", "Paint shop", "paint_shop", 22, 0, 31, 5),
    ("REWORK", "Rework bay", "rework", 32, 0, 39, 5),
    ("LANE-N", "North AGV lane", "agv_lane", 0, 6, 39, 7),
    ("SMKT", "Parts supermarket and kitting", "supermarket", 0, 8, 7, 19),
    ("TRIM", "Trim line", "line", 10, 8, 19, 12),
    ("MARR", "Marriage station", "line", 20, 8, 23, 12),
    ("FINAL", "Final assembly", "line", 24, 8, 33, 12),
    ("EOL", "End-of-line test", "line", 36, 8, 39, 15),
    ("LANE-S", "South AGV lane", "agv_lane", 10, 13, 33, 14),
    ("WALK", "Pedestrian walkway", "walkway", 10, 15, 33, 15),
    ("TEAM", "Team area and first aid", "amenities", 10, 16, 25, 19),
    ("CHG", "AGV charging", "charger", 26, 16, 39, 19),
]
ZONE_NAME = {z[0]: z[1] for z in ZONES}
BLOCKED = {**{(x, y): "rack" for y in (9, 11, 13, 15, 17) for x in range(1, 7)},
           **{(x, 10): "conveyor" for x in range(10, 34)},
           **{(x, y): "robot_cell" for x in range(11, 17) for y in range(1, 4)},
           **{(x, y): "paint_booth" for x in range(23, 31) for y in range(1, 4)}}

# station_id, name, zone, x, y, kitted, buffer_min (minutes of parts or bodies banked before the line stops)
STATIONS = [
    ("B10", "Body weld cell", "BODY", 14, 4, False, 20),
    ("B20", "Closures and hemming press", "BODY", 19, 4, False, 20),
    ("P10", "Paint booth", "PAINT", 27, 4, False, 20),
    ("T10", "Wiring harness", "TRIM", 12, 9, True, 12),
    ("T20", "Instrument panel", "TRIM", 17, 9, True, 12),
    ("M10", "Powertrain marriage", "MARR", 21, 9, True, 10),
    ("F10", "Wheels and tyres", "FINAL", 25, 9, True, 9),
    ("F20", "Fluid fill", "FINAL", 28, 9, False, 0),
    ("F30", "Seats and doors", "FINAL", 31, 9, True, 10),
    ("E10", "End-of-line test", "EOL", 37, 9, False, 0),
]
STATION = {s[0]: s for s in STATIONS}
KITTED = [s for s in STATIONS if s[5]]
LINE = [s for s in STATIONS if s[2] in ("TRIM", "MARR", "FINAL", "EOL")]
HOT_STATION = "F10"
SMKT_EXIT = (7, 13)
SPILL_KITS = [("SK-1", 2, 4), ("SK-2", 36, 2)]  # where the spill kits are kept today

# machine_id, name, kind, station_id, commissioned_year
MACHINES = [
    ("M-01", "Body weld robots", "weld_robot", "B10", 2014),
    ("M-02", "Hemming press", "press", "B20", 2011),
    ("M-03", "Paint booth robots", "paint_booth", "P10", 2016),
    ("M-04", "Trim conveyor", "conveyor", "T10", 2009),
    ("M-05", "Final conveyor", "conveyor", "F30", 2012),
    ("M-06", "Wheel nutrunner", "nutrunner", "F10", 2019),
    ("M-07", "Seat nutrunner", "nutrunner", "F30", 2021),
    ("M-08", "Marriage nutrunner", "nutrunner", "M10", 2020),
]
MACHINE = {m[0]: m for m in MACHINES}
VIBRATION_MACHINE = "M-06"  # the wheel nutrunner carries the real Bosch vibration evidence
VIBRATION_COLS = ([("recording", "TEXT PRIMARY KEY"), ("bosch_machine", "TEXT"), ("operation", "TEXT"),
                   ("period", "TEXT"), ("label", "TEXT"), ("duration_s", "REAL")]
                  + [(f"{a}_{s}", "REAL") for a in "xyz" for s in ("rms", "peak", "crest", "kurtosis")]
                  + [("linked_machine_id", "TEXT")])

# agv_id, model, kind, payload_kg, speed_mps, commissioned
AGVS = [
    ("AGV-1", "Kitting tugger KT400", "kitting", 400, 1.1, date(2023, 3, 1)),
    ("AGV-2", "Kitting tugger KT400", "kitting", 400, 1.1, date(2023, 3, 1)),
    ("AGV-3", "Kitting tugger KT300", "kitting", 300, 1.0, date(2017, 6, 1)),
    ("AGV-4", "Kitting tugger KT400", "kitting", 400, 1.1, date(2024, 9, 1)),
    ("AGV-5", "Kitting tugger KT400", "kitting", 400, 1.1, date(2024, 9, 1)),
    ("AGV-6", "Body carrier BC2000", "body_carrier", 2000, 0.8, date(2021, 1, 1)),
    ("AGV-7", "Body carrier BC2000", "body_carrier", 2000, 0.8, date(2021, 1, 1)),
    ("AGV-8", "Body carrier BC2000", "body_carrier", 2000, 0.8, date(2022, 5, 1)),
]
FLAKY_AGV = "AGV-3"
AGV_FAULT_CODES = ["E12_WHEEL_SLIP", "E31_LIDAR_BLOCKED", "E40_BATTERY_LOW", "E52_NAV_LOST", "BLOCKED_LANE"]

# All names are invented for the simulation.
NAMES = ["Aisha Banda", "Ben Okafor", "Carla Dubois", "Dmitri Haddad", "Elena Varga", "Farid Lindqvist",
         "Grace Tanaka", "Hiro Castillo", "Ines Kowalski", "Jamal Eriksen", "Kofi Moreau", "Lena Silva",
         "Marco Yilmaz", "Nadia Petrov", "Oscar Walsh", "Petra Rahman", "Quinn Achterberg", "Rosa Novak",
         "Sanjay Fontaine", "Tara Quintero", "Umar Iversen", "Vera Gallo", "Wei Jovanovic", "Zoe Ulloa"]
CERTS = {"lockout_tagout": 1, "forklift": 3, "robot_cell_entry": 2, "first_aid": 3}  # validity in years
# role, shift, home zone, covered stations, skills
CREW = [
    ("line_operator", "A", "BODY", ["B10", "B20"], ["robot_cell_entry", "weld_inspection"]),
    ("line_operator", "A", "PAINT", ["P10"], ["paint_inspection"]),
    ("line_operator", "A", "TRIM", ["T10", "T20"], ["torque_tools", "first_aid"]),
    ("line_operator", "A", "MARR", ["M10"], ["torque_tools"]),
    ("line_operator", "A", "FINAL", ["F10", "F20"], ["torque_tools", "spill_response"]),
    ("line_operator", "A", "FINAL", ["F30", "E10"], ["torque_tools", "test_driving"]),
    ("team_leader", "A", "FINAL", [], ["andon_response", "first_aid", "spill_response", "agv_reset", "torque_tools"]),
    ("maintenance_tech", "A", "REWORK", [], ["machine_repair", "lockout_tagout", "robot_cell_entry", "agv_reset", "electrical"]),
    ("quality_inspector", "A", "EOL", [], ["quality_inspection", "torque_audit"]),
    ("material_handler", "A", "SMKT", [], ["forklift", "tugger_driving", "spill_response", "agv_reset"]),
    ("shift_supervisor", "A", "TEAM", [], ["incident_command", "first_aid", "lockout_tagout", "spill_response"]),
    ("line_operator", "B", "BODY", ["B10", "B20"], ["robot_cell_entry", "weld_inspection"]),
    ("line_operator", "B", "PAINT", ["P10"], ["paint_inspection", "first_aid"]),
    ("line_operator", "B", "TRIM", ["T10", "T20"], ["torque_tools"]),
    ("line_operator", "B", "MARR", ["M10"], ["torque_tools", "spill_response"]),
    ("line_operator", "B", "FINAL", ["F10", "F20"], ["torque_tools"]),
    ("line_operator", "B", "FINAL", ["F30", "E10"], ["torque_tools", "test_driving"]),
    ("team_leader", "B", "FINAL", [], ["andon_response", "first_aid", "spill_response", "agv_reset", "incident_command"]),
    ("maintenance_tech", "B", "REWORK", [], ["machine_repair", "lockout_tagout", "robot_cell_entry", "agv_reset"]),
    ("quality_inspector", "B", "EOL", [], ["quality_inspection", "torque_audit"]),
    ("material_handler", "B", "SMKT", [], ["forklift", "tugger_driving", "spill_response", "agv_reset"]),
    ("maintenance_tech", "N", "REWORK", [], ["machine_repair", "lockout_tagout", "robot_cell_entry", "agv_reset", "electrical"]),
    ("material_handler", "N", "SMKT", [], ["forklift", "tugger_driving", "spill_response", "agv_reset"]),
    ("shift_supervisor", "N", "TEAM", [], ["incident_command", "first_aid", "spill_response"]),
]
RESPONDER_SKILL = {"part_shortage": "tugger_driving", "robot_cell_fault": "lockout_tagout", "torque_fault": "torque_tools",
                   "machine_overheat": "machine_repair", "agv_fault": "agv_reset", "fluid_spill": "spill_response",
                   "near_miss": "incident_command", "quality_defect": "quality_inspection"}

# Fallbacks used when data/raw has no downloaded anchor. OSHA_FALLBACK is the NAICS 3361 hazard mix
# observed in the January 2015 - November 2025 Severe Injury Reports (321 reports).
OSHA_FALLBACK = {"caught_in_machinery": 83, "slip_trip_fall": 67, "vehicle_pedestrian": 59, "other": 43,
                 "struck_by_object": 31, "fall_from_height": 27, "heat_exposure": 11}
BLS_TRC_FALLBACK = 5.6    # BLS SOII 2024, NAICS 3361, total recordable cases per 100 FTE
HEINRICH = 1 + 29 + 300   # safety events per recordable injury (Heinrich's 1:29:300 heuristic)
# How each OSHA hazard group shows up on our floor
SAFETY_EVENT = {"vehicle_pedestrian": ("near_miss", "agv_pedestrian"), "caught_in_machinery": ("near_miss", "robot_cell_entry"),
                "slip_trip_fall": ("fluid_spill", None), "struck_by_object": ("near_miss", "falling_part"),
                "fall_from_height": ("near_miss", "platform_edge"), "heat_exposure": ("near_miss", "heat_stress"),
                "other": ("near_miss", "unsafe_act")}

# rule_id, reference, summary (paraphrased), check, params
SAFETY_RULES = [
    ("R-TRAIN", "29 CFR 1910.178(l)", "Only trained and evaluated operators may drive a powered industrial truck such as a forklift or tugger.",
     "requires_cert", "cert=forklift;applies_to=material_handler"),
    ("R-PED-CLEAR", "29 CFR 1910.178(m)(1)", "Do not drive a truck toward a person standing in front of a fixed object.",
     "min_separation", "cells=2"),
    ("R-CROSSING", "29 CFR 1910.178(n)(4)", "Slow down and sound the horn at cross aisles and where vision is obstructed.",
     "max_speed_at", "zones=X-EAST,X-WEST;speed_mps=0.5"),
    ("R-SAFE-SPEED", "29 CFR 1910.178(n)(8)", "Operate at a speed that allows a safe stop under the conditions.",
     "max_speed", "speed_mps=1.5"),
    ("R-SURFACE", "29 CFR 1910.22(a)(1)-(3)", "Keep walking-working surfaces clean, dry and free of hazards such as spills.",
     "zone_blocked_until_clear", "kinds=fluid_spill"),
    ("R-LOTO", "29 CFR 1910.147", "Servicing or repair where unexpected start-up could cause injury needs lockout/tagout by an authorised employee.",
     "requires_cert", "cert=lockout_tagout;applies_to=robot_cell_fault,machine_overheat"),
    ("R-GUARD", "29 CFR 1910.212(a)(1)", "Machine guarding must protect operators and others from point-of-operation hazards and moving parts.",
     "no_entry_while_running", "zones=BODY,PAINT"),
    ("R-PRESS", "29 CFR 1910.217", "Mechanical power presses need point-of-operation safeguarding; die setting and repair only by trained people under lockout.",
     "requires_cert", "cert=lockout_tagout;applies_to=M-02"),
    ("R-ROBOT-CELL", "Plant rule", "Before you step into a robot cell, stop the robots, lock the gate open with your own lock and carry a robot-cell entry card that is in date.",
     "requires_cert", "cert=robot_cell_entry;applies_to=B10,P10"),
]


# ---------- helpers ----------

def q(v):
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, datetime):
        return f"'{v.isoformat(sep=' ')}'"
    if isinstance(v, date):
        return f"'{v.isoformat()}'"
    return "'" + str(v).replace("'", "''") + "'"


def inserts(name, cols, rows, batch=500):
    out = []
    for i in range(0, len(rows), batch):
        values = ",\n".join("(" + ",".join(q(v) for v in r) + ")" for r in rows[i:i + batch])
        out.append(f"INSERT INTO {name} ({','.join(cols)}) VALUES\n{values};")
    return "\n".join(out)


def table(name, cols, rows):
    """CREATE TABLE plus INSERTs. cols is a list of (column, sql_type)."""
    ddl = f"CREATE TABLE {name} (" + ", ".join(f"{c} {t}" for c, t in cols) + ");"
    return "\n".join([ddl, inserts(name, [c for c, _ in cols], rows)]) if rows else ddl


def weighted(items, weights):
    return rng.choices(items, weights=weights)[0]


def poisson(lam):
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def zone_of(x, y):
    return next((z[0] for z in ZONES if z[3] <= x <= z[5] and z[4] <= y <= z[6]), "FLOOR")


def walkable(x, y):
    return 0 <= x < COLS and 0 <= y < ROWS and (x, y) not in BLOCKED


def random_cell_in(zone_id):
    z = next(z for z in ZONES if z[0] == zone_id)
    return rng.choice([(x, y) for x in range(z[3], z[5] + 1) for y in range(z[4], z[6] + 1)
                       if walkable(x, y) and zone_of(x, y) == zone_id])


def manhattan(a, b):
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def shift_of(ts):
    return "A" if 6 <= ts.hour < 14 else "B" if 14 <= ts.hour < 22 else "N"


def shift_day(ts):
    return (ts - timedelta(hours=6)).date() if ts.hour < 6 else ts.date()


# ---------- real-world anchors ----------

def load_weather():
    path = SEED / "weather_detroit_hourly.csv"
    temps = {}
    if path.exists():
        with path.open(newline="") as f:
            for r in csv.DictReader(f):
                if r["temperature_c"] not in ("", "None"):
                    temps[datetime.fromisoformat(r["ts"])] = float(r["temperature_c"])
    if len(temps) >= DAYS * 24 * 0.95:
        return temps, "open-meteo"
    print("data/seed/weather_detroit_hourly.csv missing or incomplete: synthesising Detroit-like temperatures")
    temps = {}
    for h in range(DAYS * 24):
        ts = START + timedelta(hours=h)
        seasonal = 25 - 13 * h / (DAYS * 24)  # late July to mid October
        temps[ts] = round(seasonal + 5 * math.sin((ts.hour - 9) / 24 * 2 * math.pi) + rng.gauss(0, 2), 1)
    return temps, "synthetic"


def load_osha_mix():
    path = SEED / "osha_sir_naics3361_counts.csv"
    if path.exists():
        mix = Counter()
        with path.open(newline="") as f:
            for r in csv.DictReader(f):
                mix[r["hazard_group"]] += int(r["reports"])
        if mix:
            return dict(mix), "osha-sir"
    print("data/seed/osha_sir_naics3361_counts.csv missing: using the built-in OSHA hazard mix")
    return dict(OSHA_FALLBACK), "fallback"


def load_bls_rate():
    path = SEED / "bls_soii_naics3361.csv"
    if path.exists():
        with path.open(newline="") as f:
            rows = [r for r in csv.DictReader(f) if r["measure"] == "total_recordable_rate_per_100_fte"]
        if rows:
            latest = max(rows, key=lambda r: r["year"])
            return float(latest["value"]), f"bls-soii-{latest['year']}"
    print("data/seed/bls_soii_naics3361.csv missing: using BLS 2024 rate 5.6")
    return BLS_TRC_FALLBACK, "fallback"


def load_vibration():
    """Bosch CNC Machining recordings summarised to one row each (see fetch_sources.py)."""
    path = SEED / "bosch_cnc_vibration_summary.csv"
    if not path.exists():
        print("data/seed/bosch_cnc_vibration_summary.csv missing: torque faults get no vibration evidence")
        return []
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def ai4i_rows():
    path = SEED / "ai4i2020.csv"
    if path.exists():
        with path.open(newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                yield (float(r["Air temperature [K]"]), float(r["Process temperature [K]"]),
                       int(r["Rotational speed [rpm]"]), float(r["Torque [Nm]"]), int(r["Tool wear [min]"]),
                       int(r["Machine failure"]), int(r["HDF"]), int(r["OSF"]), r["Type"], "ai4i2020")
        return
    print("data/seed/ai4i2020.csv not found: synthesising machine telemetry instead")
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


# ---------- people ----------

def make_workers():
    workers = []
    for i, (role, shift, zone, stations, skills) in enumerate(CREW):
        x, y = (STATION[stations[0]][3], STATION[stations[0]][4]) if stations else random_cell_in(zone)
        workers.append({"id": f"W-{i + 1:02d}", "name": NAMES[i], "role": role, "shift": shift, "zone": zone,
                        "stations": stations, "skills": skills, "x": x, "y": y,
                        "hired": (START - timedelta(days=rng.randint(90, 4000))).date()})
    return workers


def make_certs(workers):
    certs = []
    for w in workers:
        for skill in w["skills"]:
            if skill in CERTS:
                expires = END.date() + timedelta(days=rng.randint(20, 365 * CERTS[skill]))
                certs.append([w["id"], skill, expires - timedelta(days=365 * CERTS[skill]), expires])
    # Planted lapses: the night technician's lockout/tagout (mid-quarter) and a B-shift forklift card.
    lapses = {("N", "maintenance_tech", "lockout_tagout"): 40, ("B", "material_handler", "forklift"): 25}
    for c in certs:
        w = next(w for w in workers if w["id"] == c[0])
        if (w["shift"], w["role"], c[1]) in lapses:
            c[3] = START.date() + timedelta(days=lapses[(w["shift"], w["role"], c[1])])
            c[2] = c[3] - timedelta(days=365 * CERTS[c[1]])
    return certs


def make_roster(workers):
    rows, present = [], {}
    for d in range(DAYS):
        day = (START + timedelta(days=d)).date()
        saturday_ot = day.weekday() == 5 and rng.random() < 0.35
        codes = ["A", "B", "N"] if day.weekday() < 5 else ["A"] if saturday_ot else []
        for code in codes:
            h0, h1 = SHIFTS[code]
            here = []
            for w in workers:
                if w["shift"] != code:
                    continue
                if day.weekday() == 5:
                    status = "overtime" if rng.random() < 0.85 else "off"
                else:
                    r = rng.random()
                    status = "on" if r < 0.9 else "absent" if r < 0.96 else "leave"
                if status == "off":
                    continue
                rows.append((w["id"], day, code, f"{h0:02d}:00", f"{h1 % 24:02d}:00", status))
                if status in ("on", "overtime"):
                    here.append(w)
            present[(day, code)] = here
    return rows, present


def has_skill(w, skill, day, cert_expiry, check_cert=True):
    if skill not in w["skills"]:
        return False
    return not (check_cert and skill in CERTS) or cert_expiry[(w["id"], skill)] >= day


def pick_responder(crew, skill, at, day, cert_expiry, check_cert=True):
    able = sorted((w for w in crew if has_skill(w, skill, day, cert_expiry, check_cert)),
                  key=lambda w: manhattan((w["x"], w["y"]), at))
    if not able:
        return None
    return able[1] if len(able) > 1 and rng.random() < 0.25 else able[0]  # the nearest is sometimes busy


# ---------- fleet routes ----------

def route(agv_kind):
    """Waypoints for one trip, following the AGV lanes and never crossing the conveyor."""
    if agv_kind == "kitting":
        sx, sy = random_cell_in("SMKT")
        st = weighted(KITTED, [3 if s[0] == HOT_STATION else 1 for s in KITTED])
        dx, dy = st[3], 11 if st[2] != "BODY" else 6  # line-side racks south of the conveyor
        return "kit_delivery", "SMKT", st[0], [(sx, sy), (7, sy), SMKT_EXIT, (dx, 13), (dx, dy)], rng.randint(80, 300)
    if rng.random() < 0.85:  # painted body down the west crossing to the start of trim
        return "body_transfer", "PAINT", "T10", [(29, 5), (29, 7), (9, 7), (9, 11), (12, 11)], 1450
    dx, dy = random_cell_in("REWORK")  # a defective car from end-of-line to the rework bay
    return "rework_transfer", "EOL", "REWORK", [(37, 12), (37, 7), (dx, 7), (dx, dy)], 1550


def path_len(points):
    return sum(manhattan(a, b) for a, b in zip(points, points[1:]))


def point_at(points, frac):
    d = frac * path_len(points)
    for a, b in zip(points, points[1:]):
        seg = manhattan(a, b)
        if seg and d <= seg:
            f = d / seg
            return round(a[0] + (b[0] - a[0]) * f), round(a[1] + (b[1] - a[1]) * f)
        d -= seg
    return points[-1]


# ---------- the hour-by-hour simulation ----------

class Plant:
    def __init__(self):
        self.temps, self.weather_source = load_weather()
        self.osha_mix, self.osha_source = load_osha_mix()
        self.bls_rate, self.bls_source = load_bls_rate()
        self.workers = make_workers()
        self.certs = make_certs(self.workers)
        self.cert_expiry = {(c[0], c[1]): c[3] for c in self.certs}
        self.roster, self.present = make_roster(self.workers)
        self.ai4i = list(ai4i_rows())
        self.vibration = load_vibration()
        self.vibration_by_label = {lab: [v["recording"] for v in self.vibration if v["label"] == lab]
                                   for lab in ("normal", "abnormal")}
        self.agvs = [{"id": a[0], "model": a[1], "kind": a[2], "payload": a[3], "speed": a[4], "commissioned": a[5],
                      "home": (27 + i, 17 + i % 2),
                      "last_service": (END - timedelta(days=150 if a[0] == FLAKY_AGV else rng.randint(4, 25))).date(),
                      "health": 62 if a[0] == FLAKY_AGV else rng.randint(84, 97)} for i, a in enumerate(AGVS)]
        self.down = defaultdict(list)  # agv_id -> [(start, end, fault_code, (x, y))]
        self.incidents, self.hourly = [], []
        # BLS rate x FTE x time -> expected recordables; Heinrich ratio -> safety events in the window.
        fte = len(self.workers)
        self.expected_recordables = fte * self.bls_rate / 100 * DAYS / 365
        self.safety_events = self.expected_recordables * HEINRICH
        self.machine_heat = {m[0]: 1 + sum(r[6] for r in self.machine_slice(i)) for i, m in enumerate(MACHINES)}

    def machine_slice(self, i):
        n = DAYS * 24
        return [self.ai4i[(i * 1250 + k) % len(self.ai4i)] for k in range(n)]

    def run(self):
        hours = DAYS * 24

        def safety_weight(ts):  # safety events cluster around shift changes and production
            crew_on = (shift_day(ts), shift_of(ts)) in self.present
            producing_now = crew_on and shift_of(ts) in PRODUCTION_SHIFTS
            return 0.0 if not crew_on else 2.0 if ts.hour in (6, 13, 14, 21) else 1.0 if producing_now else 0.4

        total_weight = sum(safety_weight(START + timedelta(hours=h)) for h in range(hours))
        mix_groups = list(self.osha_mix)
        mix_weights = [self.osha_mix[g] for g in mix_groups]
        for h in range(hours):
            ts = START + timedelta(hours=h)
            code, day = shift_of(ts), shift_day(ts)
            crew = self.present.get((day, code))
            producing = crew is not None and code in PRODUCTION_SHIFTS
            temp = self.temps.get(ts, 20.0)
            # AGV faults first, so this hour's fleet capacity reflects them
            lam = 0.2 if producing else 0.05 if crew is not None else 0.0
            for _ in range(poisson(lam)):
                self.incident("agv_fault", ts, crew, temp, 0)
            mid = ts + timedelta(minutes=30)
            available = sum(1 for a in self.agvs if a["kind"] == "kitting"
                            and not any(w[0] <= mid < w[1] for w in self.down[a["id"]]))
            if producing:
                into_shift = ts.hour - SHIFTS[code][0]
                demand = 3.7 + 0.6 * math.sin(into_shift / 8 * math.pi) + rng.gauss(0, 0.3)
                demand *= 0.75 if into_shift == 4 else 1.0  # meal break
                util = min(1.0, demand / max(available, 1))
            else:
                demand, util = 0.0, 0.0
            self.hourly.append((ts, code, producing, round(demand, 2), available, round(util * 100, 1), temp))
            if producing:
                rates = {
                    "part_shortage": 0.56 * (1 + 4 * max(0.0, util - 0.75)) * (1.6 if ts.weekday() == 0 and code == "A" else 1),
                    "robot_cell_fault": 0.23,
                    "torque_fault": 0.21,
                    "quality_defect": 0.22,
                    "machine_overheat": 0.05 * math.exp((temp - 20) / 6),
                    "fluid_spill": 0.05,  # leaks at the fluid fill rig and powertrain marriage
                }
            elif crew is not None:
                rates = {"robot_cell_fault": 0.02, "machine_overheat": 0.006 * math.exp((temp - 20) / 6),
                         "fluid_spill": 0.01}
            else:
                rates = {}
            for kind, lam in rates.items():
                for _ in range(poisson(lam)):
                    self.incident(kind, ts, crew, temp, util)
            if crew is not None:  # OSHA-weighted safety events; the BLS rate sets how many
                for _ in range(poisson(self.safety_events * safety_weight(ts) / total_weight)):
                    group = weighted(mix_groups, mix_weights)
                    kind, cause = SAFETY_EVENT.get(group, SAFETY_EVENT["other"])
                    self.incident(kind, ts, crew, temp, util, cause=cause, osha_group=group)
        self.incidents.sort(key=lambda r: r["ts"])
        recordables = poisson(self.expected_recordables)
        safety = [i for i in self.incidents if i["osha_group"]]
        for inc in rng.sample(safety, min(recordables, len(safety))):
            inc["recordable_injury"] = True
            inc["description"] += "; recordable injury"
        for n, inc in enumerate(self.incidents, 1):
            inc["incident_id"] = f"INC-{n:05d}"

    def incident(self, kind, hour_ts, crew, temp, util, cause=None, osha_group=None):
        ts = hour_ts + timedelta(seconds=rng.randint(0, 3599))
        code, day = shift_of(ts), shift_day(ts)
        producing = code in PRODUCTION_SHIFTS
        station = machine = agv = None
        zone = None
        if kind == "part_shortage":
            station = weighted([s[0] for s in KITTED], [3 if s[0] == HOT_STATION else 1 for s in KITTED])
            cause = weighted(["agv_late", "mispick", "supplier_short", "agv_down"], [4, 2, 1, 1 + 3 * util])
            if cause in ("agv_late", "agv_down"):
                agv = weighted([a for a in self.agvs if a["kind"] == "kitting"],
                               [4 if a["id"] == FLAKY_AGV else 1 for a in self.agvs if a["kind"] == "kitting"])
        elif kind == "robot_cell_fault":
            machine = weighted(["M-01", "M-02", "M-03"], [5, 2, 3])
            cause = {"M-01": ["tip_dress_failure", "servo_overcurrent", "weld_spatter_sensor", "light_curtain_trip"],
                     "M-02": ["die_misfeed", "hydraulic_pressure_low"],
                     "M-03": ["atomiser_clog", "colour_change_fault", "booth_door_interlock"]}[machine]
            cause = rng.choice(cause)
        elif kind == "torque_fault":
            machine = weighted(["M-06", "M-07", "M-08"], [4, 1, 1])
            cause = rng.choice(["torque_out_of_range", "angle_out_of_range", "socket_worn", "controller_fault"])
        elif kind == "machine_overheat":
            ids = [m[0] for m in MACHINES]
            machine = weighted(ids, [self.machine_heat[i] for i in ids])
            cause = "temperature_alarm"
        elif kind == "agv_fault":
            agv = weighted(self.agvs, [6 if a["id"] == FLAKY_AGV else 1 for a in self.agvs])
            cause = weighted(AGV_FAULT_CODES, [3, 2, 4 if agv["id"] == FLAKY_AGV else 1, 2, 2])
            zone = weighted(["LANE-N", "LANE-S", "X-WEST", "X-EAST", "SMKT"], [3, 4, 2, 1, 2])
        elif kind == "fluid_spill":
            if osha_group is None and rng.random() < 0.75:
                station = weighted(["F20", "M10"], [3, 1])
            else:
                zone = weighted(["FINAL", "MARR", "TRIM", "LANE-S", "SMKT", "RECV"], [3, 2, 1, 2, 1, 1])
            cause = (weighted(["coolant", "brake_fluid", "washer_fluid"], [4, 3, 2]) if station == "F20"
                     else weighted(["hydraulic_oil", "gear_oil", "coolant"], [3, 2, 1]))
        elif kind == "near_miss":
            if cause == "agv_pedestrian":
                zone = weighted(["X-WEST", "X-EAST", "LANE-S", "LANE-N"], [6, 2, 1, 1])
                busy = [a for a in self.agvs if a["kind"] == "body_carrier"] if zone == "X-WEST" else self.agvs
                agv = rng.choice(busy)
            else:
                zone = {"robot_cell_entry": "BODY", "falling_part": "SMKT", "platform_edge": "MARR",
                        "heat_stress": "PAINT", "unsafe_act": "FINAL"}[cause]
        else:  # quality_defect
            station = weighted([s[0] for s in LINE], [2 if s[0] == HOT_STATION else 1 for s in LINE])
            cause = rng.choice(["torque_out_of_spec", "paint_run", "missing_clip", "seal_gap", "connector_not_seated"])
        if machine:
            station = MACHINE[machine][3]
        if station:
            st = STATION[station]
            zone, x, y = st[2], st[3] + (rng.randint(-1, 1) if kind == "fluid_spill" else 0), st[4]
        else:
            x, y = random_cell_in(zone)

        # Repairs sometimes went to whoever was free without checking certifications, mostly at night.
        check_cert = not (kind in ("robot_cell_fault", "machine_overheat") and rng.random() < (0.5 if code == "N" else 0.1))
        responder = pick_responder(crew or [], RESPONDER_SKILL[kind], (x, y), day, self.cert_expiry, check_cert)
        penalty = 0
        if responder is None:  # nobody qualified on shift: a leader, or the nearest person, calls someone in
            leaders = [w for w in crew or [] if w["role"] in ("shift_supervisor", "team_leader")]
            nearby = sorted(crew or [], key=lambda w: manhattan((w["x"], w["y"]), (x, y)))
            responder = (leaders or nearby or [None])[0]
            penalty = 900
        walk_s = manhattan((responder["x"], responder["y"]), (x, y)) * CELL_M / 1.2 if responder else 300
        response_s = int(walk_s + rng.randint(20, 90) + penalty / 3)
        smkt_distance = manhattan(SMKT_EXIT, (x, y)) if kind == "part_shortage" else None
        kit_distance = min(manhattan((kx, ky), (x, y)) for _, kx, ky in SPILL_KITS) if kind == "fluid_spill" else None

        false_alarm = (rng.random() < 0.25 if kind == "machine_overheat" else
                       rng.random() < 0.2 if machine == VIBRATION_MACHINE else  # noisy torque/vibration sensor
                       rng.random() < 0.04 if kind in ("robot_cell_fault", "torque_fault", "agv_fault") else False)
        # The wheel nutrunner's alarms come with a real Bosch vibration recording as evidence:
        # an abnormal one for genuine faults, a normal one when it turns out to be sensor noise.
        recording = None
        if machine == VIBRATION_MACHINE and self.vibration:
            pool = self.vibration_by_label["normal" if false_alarm else "abnormal"]
            recording = rng.choice(pool) if pool else None
        if kind == "part_shortage":
            clear = ({"agv_late": 240, "mispick": 360, "supplier_short": 1500, "agv_down": 420}[cause]
                     + smkt_distance * 10)  # walk to the supermarket, pick, drive back
        elif kind == "robot_cell_fault":
            clear = {"M-01": 1200, "M-02": 1800, "M-03": 1400}[machine]
        elif kind == "torque_fault":
            clear = 480 if cause != "controller_fault" else 1200
        elif kind == "machine_overheat":
            clear = 900 + 60 * max(0.0, temp - 25)
        elif kind == "agv_fault":
            clear = 420 + (900 if cause == "E40_BATTERY_LOW" and agv["id"] == FLAKY_AGV else 0)
        elif kind == "fluid_spill":
            clear = 300 + kit_distance * 12  # fetch the kit, often twice
        elif kind == "near_miss":
            clear = 900  # stop, make safe, file the report
        else:
            clear = 600
        clear = clear + walk_s + penalty
        if responder and RESPONDER_SKILL[kind] == "lockout_tagout" and responder["role"] != "maintenance_tech":
            clear += 900
        clear = max(60.0, clear * rng.uniform(0.8, 1.25))
        if code == "N":
            clear *= 1.35  # one technician, tired, parts store closed
        if false_alarm:
            clear = rng.randint(120, 420)  # someone still has to go and check

        if not producing:
            downtime = 0.0
        elif false_alarm:
            downtime = clear * 0.5 if kind == "machine_overheat" and station in [s[0] for s in LINE] else 0.0
        elif kind == "part_shortage":
            downtime = max(0.0, clear - STATION[station][6] * 60 * rng.uniform(0.8, 1.8))  # stock on hand varies
        elif kind == "robot_cell_fault":
            downtime = max(0.0, clear - STATION[station][6] * 60)
        elif kind in ("torque_fault", "machine_overheat"):
            bank = STATION[station][6] * 60 if zone in ("BODY", "PAINT") else 0
            downtime = max(0.0, clear - bank) * (0.6 if kind == "torque_fault" else 1.0)  # manual torque backup
        elif kind == "agv_fault":
            downtime = max(0.0, clear - 300) if agv["kind"] == "body_carrier" else 0.0
        elif kind == "fluid_spill":
            downtime = clear * 0.5 if station else 0.0
        elif kind == "near_miss":
            downtime = 300.0 if rng.random() < 0.15 else 0.0  # safety stand-down
        else:
            downtime = 0.0
        cars_lost = round(downtime / TAKT_S)
        cost = (downtime / 60 * LINE_STOP_USD_PER_MIN + clear / 3600 * LABOUR_USD_PER_HOUR
                + (REWORK_USD if kind == "quality_defect" else 0)
                + (rng.randint(50, 900) if kind in ("robot_cell_fault", "torque_fault") and not false_alarm else 0))
        severity = ("low" if false_alarm else "high" if downtime >= 900 or clear >= 2700 or kind == "near_miss" and cause == "robot_cell_entry"
                    else "medium" if downtime > 0 else "low")
        outcome = ("false_alarm" if false_alarm else "rework" if kind == "quality_defect" else "reported" if kind == "near_miss"
                   else "escalated" if clear > 3600 else "resolved")
        operators = [w for w in crew or [] if station in w["stations"]]
        reporter = operators[0] if operators else (rng.choice(crew) if crew else None)
        agv_id = agv["id"] if agv else None
        where = f"{station} {STATION[station][1]}" if station else ZONE_NAME[zone]
        pretty = cause.replace("_", " ")
        description = {
            "part_shortage": f"Parts shortage at {where}: {pretty}" + (f" ({agv_id})" if agv_id else ""),
            "robot_cell_fault": f"{MACHINE[machine][1] if machine else ''} at {where} stopped: {pretty}",
            "torque_fault": f"{MACHINE[machine][1] if machine else ''} at {where}: {pretty}",
            "machine_overheat": f"{MACHINE[machine][1] if machine else ''} at {where}: temperature alarm"
                                + (" (sensor noise, no fault found)" if false_alarm else ""),
            "agv_fault": (f"{agv_id} held by a blocked lane in {ZONE_NAME[zone]}" if cause == "BLOCKED_LANE"
                          else f"{agv_id} stopped in {ZONE_NAME[zone]} with {cause}"),
            "fluid_spill": f"{pretty.capitalize()} spill at {where}; area cordoned",
            "near_miss": {"agv_pedestrian": f"{agv_id} came within 1 m of a pedestrian at {where}",
                          "robot_cell_entry": "Operator reached into the weld cell guard opening while robots were in auto",
                          "falling_part": "Part fell from an upper rack level in the supermarket",
                          "platform_edge": "Operator stepped near the open edge of the marriage platform",
                          "heat_stress": "Painter reported dizziness in the hot paint booth",
                          "unsafe_act": "Operator bypassed a station guard to clear a jam"}.get(cause, pretty),
            "quality_defect": f"{pretty.capitalize()} found at {where}; unit to rework",
        }[kind]
        if agv_id and kind in ("agv_fault", "part_shortage") and not false_alarm and (kind == "agv_fault" or cause == "agv_down"):
            code_ = cause if kind == "agv_fault" else "E52_NAV_LOST"
            self.down[agv_id].append((ts, ts + timedelta(seconds=clear), code_, (x, y)))
        self.incidents.append({
            "ts": ts, "shift_code": code, "kind": kind, "cause": cause, "zone_id": zone, "station_id": station,
            "machine_id": machine, "agv_id": agv_id, "x": x, "y": y, "severity": severity,
            "reported_by": reporter["id"] if reporter else None, "worker_id": responder["id"] if responder else None,
            "response_s": response_s, "time_to_clear_s": int(clear), "downtime_min": round(downtime / 60, 1),
            "cars_lost": cars_lost, "cost_usd": round(cost), "outcome": outcome, "false_alarm": false_alarm,
            "supermarket_distance": smkt_distance, "spill_kit_distance": kit_distance,
            "outdoor_temp_c": temp, "agv_utilisation_pct": round(util * 100, 1) if producing else None,
            "osha_group": osha_group, "recordable_injury": False, "vibration_recording": recording,
            "description": description})

    # ---------- derived tables ----------

    def andon(self):
        rows = []
        for (day, code), crew in self.present.items():
            if code not in PRODUCTION_SHIFTS:
                continue
            start = datetime.combine(day, time(SHIFTS[code][0]))
            leader = next((w for w in crew if w["role"] in ("team_leader", "shift_supervisor")), None)
            for st in STATIONS:
                for _ in range(poisson(0.8)):
                    response = max(10, int(rng.gauss(50, 20))) + (0 if leader else 90)
                    stopped = rng.random() < 0.04
                    resolved = response + max(20, int(rng.gauss(110, 50))) + (rng.randint(60, 240) if stopped else 0)
                    reason = weighted(["help_needed", "missing_part", "quality_check", "tool_issue", "safety"], [4, 2, 3, 2, 0.5])
                    rows.append([start + timedelta(seconds=rng.randint(0, 8 * 3600 - 1)), code, st[0], reason,
                                 leader["id"] if leader else None, response, resolved, stopped, None])
        reason_of = {"part_shortage": "missing_part", "robot_cell_fault": "equipment", "torque_fault": "equipment",
                     "machine_overheat": "equipment", "fluid_spill": "safety", "quality_defect": "quality_check"}
        for inc in self.incidents:
            if inc["station_id"] and inc["shift_code"] in PRODUCTION_SHIFTS and inc["kind"] in reason_of:
                rows.append([inc["ts"], inc["shift_code"], inc["station_id"], reason_of[inc["kind"]], inc["worker_id"],
                             inc["response_s"], inc["time_to_clear_s"], inc["downtime_min"] > 0, inc["incident_id"]])
        rows.sort(key=lambda r: (r[0], r[2]))
        return [[f"AND-{n:05d}"] + r for n, r in enumerate(rows, 1)]

    def production(self, andon):
        stop_s, counts, quality = defaultdict(float), defaultdict(int), defaultdict(list)
        for inc in self.incidents:
            key = (shift_day(inc["ts"]), inc["shift_code"])
            stop_s[key] += inc["downtime_min"] * 60
            counts[key] += 1
            if inc["kind"] == "quality_defect":
                quality[key].append(inc)
        pulls = Counter()
        for a in andon:
            key = (a[1].date(), a[2])
            pulls[key] += 1
            if a[8] and a[9] is None:  # routine pulls that stopped the line
                stop_s[key] += a[7]
        plan, units = [], []
        planned = int(7.5 * 3600 // TAKT_S)  # 30 minutes of breaks per shift
        for key in self.present:
            day, code = key
            if code not in PRODUCTION_SHIFTS:
                continue
            start = datetime.combine(day, time(SHIFTS[code][0]))
            built = max(0, planned - round(stop_s[key] / TAKT_S) - rng.randint(0, 5))
            plan.append((day, code, planned, built, round(stop_s[key] / 60, 1), counts[key], pulls[key],
                         day.weekday() == 5, TAKT_S))
            step = 7.5 * 3600 / max(built, 1)
            shift_units = []
            for i in range(built):
                model = weighted([("SF-S1", "sedan"), ("SF-X3", "suv"), ("SF-V2", "van")], [55, 35, 10])
                hold = rng.random() < 0.01
                shift_units.append([f"SF{day:%y%m%d}{code}{i + 1:03d}", model[0], model[1],
                                    rng.choice(["base", "comfort", "sport"]),
                                    rng.choice(["arctic white", "graphite", "midnight blue", "signal red", "silver"]),
                                    day, code, start + timedelta(minutes=30, seconds=int(i * step)),
                                    "hold" if hold else "shipped", "audit_hold" if hold else None])
            for inc in quality[key]:
                unit = next((u for u in shift_units if u[7] >= inc["ts"]), shift_units[-1] if shift_units else None)
                if unit:
                    unit[8], unit[9] = "rework", inc["cause"]
            units += shift_units
        return plan, units

    def work_orders(self):
        rows = []
        for inc in self.incidents:
            if inc["kind"] in ("robot_cell_fault", "torque_fault", "machine_overheat", "agv_fault") and not inc["false_alarm"]:
                closed = inc["ts"] + timedelta(seconds=inc["time_to_clear_s"] + rng.randint(300, 3600))
                done = closed < END
                rows.append([inc["ts"], "machine" if inc["machine_id"] else "agv", inc["machine_id"] or inc["agv_id"],
                             "reactive", "high" if inc["severity"] == "high" else "medium", inc["worker_id"],
                             "closed" if done else "open", closed if done else None,
                             inc["time_to_clear_s"] // 60 + rng.randint(10, 60), inc["incident_id"], inc["cause"]])
        techs = [w["id"] for w in self.workers if w["role"] == "maintenance_tech"]
        for m in MACHINES:
            t = END - timedelta(days=rng.randint(1, 14))
            while t > START:
                when = datetime.combine(t.date(), time(23))
                labour = rng.randint(60, 180)
                rows.append([when, "machine", m[0], "planned", "low", rng.choice(techs), "closed",
                             when + timedelta(minutes=labour), labour, None, "preventive maintenance"])
                t -= timedelta(days=14)
        for a in self.agvs:
            last = datetime.combine(a["last_service"], time(23))
            t = last
            while t > START:
                labour = rng.randint(45, 120)
                rows.append([t, "agv", a["id"], "planned", "low", rng.choice(techs), "closed",
                             t + timedelta(minutes=labour), labour, None, "preventive maintenance"])
                t -= timedelta(days=28)
            due = last + timedelta(days=28)
            while due < END:
                if due >= START:
                    rows.append([due, "agv", a["id"], "planned", "medium", None, "overdue", None, None, None,
                                 "preventive maintenance not done"])
                due += timedelta(days=28)
        rows.sort(key=lambda r: (r[0], r[2]))
        return [[f"WO-{n:05d}"] + r for n, r in enumerate(rows, 1)]

    def tasks(self):
        """AGV trips sized to each hour's kitting demand, skipping the hours an AGV is down."""
        rows = []
        for ts, code, producing, demand, available, util, _ in self.hourly:
            if not producing:
                continue
            for a in self.agvs:
                busy = util / 100 if a["kind"] == "kitting" else 0.55
                t = ts + timedelta(seconds=rng.randint(0, 240))
                hour_end = ts + timedelta(hours=1)
                while t < hour_end - timedelta(minutes=3):
                    down = next((w for w in self.down[a["id"]] if w[0] <= t < w[1]), None)
                    if down:
                        t = down[1] + timedelta(seconds=rng.randint(60, 300))
                        continue
                    kind, src, dest, points, payload = route(a["kind"])
                    travel = path_len(points) * CELL_M / a["speed"]
                    delay = rng.randint(60, 420) if rng.random() < 0.04 + 0.15 * max(0.0, busy - 0.8) else 0
                    duration = 2 * travel + rng.randint(60, 150) + delay
                    finish = t + timedelta(seconds=int(duration))
                    status = "late" if delay else "done"
                    fault = next((w for w in self.down[a["id"]] if t < w[0] < finish), None)
                    if fault:
                        finish, status = fault[0], "aborted"
                    rows.append({"agv": a["id"], "type": kind, "shift": code, "from": src, "to": dest, "start": t,
                                 "end": finish, "distance_m": int(2 * path_len(points) * CELL_M), "payload": payload,
                                 "status": status, "delay": delay, "path": points})
                    idle = duration * (1 - busy) / max(busy, 0.05)
                    t = finish + timedelta(seconds=int(idle * rng.uniform(0.6, 1.4)))
        rows.sort(key=lambda r: (r["start"], r["agv"]))
        for n, r in enumerate(rows, 1):
            r["id"] = f"T-{n:06d}"
        return rows

    def agv_telemetry(self, tasks):
        t0 = END - timedelta(days=TELEMETRY_DAYS)
        by_agv = defaultdict(list)
        for task in tasks:
            if task["end"] > t0:
                by_agv[task["agv"]].append(task)
        rows = []
        for a in self.agvs:
            mine, faults = by_agv[a["id"]], [w for w in self.down[a["id"]] if w[1] > t0]
            battery, charging, i, state = rng.uniform(70, 95), False, 0, "idle"
            drain = (0.16 if a["kind"] == "body_carrier" else 0.11) * 100 / a["health"]
            for m in range(TELEMETRY_DAYS * 1440):
                now = t0 + timedelta(minutes=m)
                while i < len(mine) and mine[i]["end"] <= now:
                    i += 1
                fault = next((f for f in faults if f[0] <= now < f[1]), None)
                task = mine[i] if i < len(mine) and mine[i]["start"] <= now else None
                speed, code, task_id = 0.0, None, None
                if fault:
                    state, code, (x, y) = "fault", fault[2], fault[3]
                    battery -= 0.01
                elif task:
                    task_id = task["id"]
                    phase = (now - task["start"]) / (task["end"] - task["start"])
                    if 0.45 <= phase < 0.55:
                        state, frac = "handling", 1.0
                        battery -= 0.03
                    else:
                        state, frac = "moving", phase / 0.45 if phase < 0.45 else 1 - (phase - 0.55) / 0.45
                        battery -= drain
                    x, y = point_at(task["path"], frac)
                    if state == "moving":
                        if zone_of(x, y) in ("X-EAST", "X-WEST"):  # body carriers do not slow down
                            speed = rng.uniform(0.7, 1.0) if a["kind"] == "body_carrier" else rng.uniform(0.3, 0.5)
                        else:
                            speed = a["speed"] * rng.uniform(0.85, 1.1)
                else:
                    x, y = a["home"]
                    charging = charging or battery < 35
                    if charging:
                        state, battery = "charging", battery + 0.9
                        charging = battery < 95
                    else:
                        state, battery = "idle", battery - 0.01
                battery = min(100.0, max(3.0, battery))
                rows.append((now, a["id"], x, y, round(speed, 2), round(battery, 1), state, code, task_id))
            a["battery_now"], a["state_now"] = round(battery), state
        return rows

    def machine_telemetry(self):
        """AI4I rows, one per machine per hour for 90 days; air and process temperatures follow the weather."""
        rows = []
        for i, m in enumerate(MACHINES):
            for h, r in enumerate(self.machine_slice(i)):
                ts = START + timedelta(hours=h)
                offset = 0.25 * (self.temps.get(ts, 20.0) - 20)
                rows.append((ts, m[0], round(r[0] + offset, 1), round(r[1] + offset, 1), *r[2:8], r[9]))
        return rows


# ---------- SQL ----------

def build():
    plant = Plant()
    plant.run()
    andon = plant.andon()
    plan, units = plant.production(andon)
    work_orders = plant.work_orders()
    tasks = plant.tasks()
    agv_telemetry = plant.agv_telemetry(tasks)
    machine_telemetry = plant.machine_telemetry()
    incidents = plant.incidents
    faults = Counter(i["agv_id"] for i in incidents if i["kind"] == "agv_fault" and not i["false_alarm"])

    fleet = [
        table("agvs", [("agv_id", "TEXT PRIMARY KEY"), ("model", "TEXT"), ("kind", "TEXT"), ("home_x", "INT"),
                       ("home_y", "INT"), ("battery_pct", "INT"), ("battery_health_pct", "INT"), ("status", "TEXT"),
                       ("max_payload_kg", "INT"), ("top_speed_mps", "REAL"), ("commissioned_date", "DATE"),
                       ("last_service_date", "DATE"), ("faults_90d", "INT")],
              [(a["id"], a["model"], a["kind"], *a["home"], a["battery_now"], a["health"], a["state_now"], a["payload"],
                a["speed"], a["commissioned"], a["last_service"], faults[a["id"]]) for a in plant.agvs]),
        table("agv_tasks", [("task_id", "TEXT PRIMARY KEY"), ("agv_id", "TEXT"), ("task_type", "TEXT"),
                            ("shift_code", "TEXT"), ("from_zone", "TEXT"), ("to_location", "TEXT"),
                            ("start_ts", "TIMESTAMP"), ("end_ts", "TIMESTAMP"), ("distance_m", "INT"),
                            ("payload_kg", "INT"), ("status", "TEXT"), ("delay_s", "INT")],
              [(t["id"], t["agv"], t["type"], t["shift"], t["from"], t["to"], t["start"], t["end"], t["distance_m"],
                t["payload"], t["status"], t["delay"]) for t in tasks]),
        table("agv_telemetry", [("ts", "TIMESTAMP"), ("agv_id", "TEXT"), ("x", "INT"), ("y", "INT"),
                                ("speed_mps", "REAL"), ("battery_pct", "REAL"), ("state", "TEXT"),
                                ("fault_code", "TEXT"), ("task_id", "TEXT")], agv_telemetry),
        table("fleet_hourly", [("ts", "TIMESTAMP"), ("shift_code", "TEXT"), ("producing", "BOOLEAN"),
                               ("kitting_demand_agvs", "REAL"), ("kitting_agvs_available", "INT"),
                               ("utilisation_pct", "REAL"), ("outdoor_temp_c", "REAL")], plant.hourly),
    ]
    hr = [
        table("workers", [("worker_id", "TEXT PRIMARY KEY"), ("name", "TEXT"), ("role", "TEXT"), ("shift_code", "TEXT"),
                          ("station_id", "TEXT"), ("home_zone", "TEXT"), ("skills", "TEXT"),
                          ("forklift_licence", "BOOLEAN"), ("x", "INT"), ("y", "INT"), ("hire_date", "DATE")],
              [(w["id"], w["name"], w["role"], w["shift"], ",".join(w["stations"]) or None, w["zone"],
                ",".join(w["skills"]), "forklift" in w["skills"], w["x"], w["y"], w["hired"]) for w in plant.workers]),
        table("certifications", [("worker_id", "TEXT"), ("cert", "TEXT"), ("issued_date", "DATE"),
                                 ("expires_date", "DATE")], plant.certs),
        table("shifts", [("worker_id", "TEXT"), ("shift_date", "DATE"), ("shift_code", "TEXT"),
                         ("start_time", "TEXT"), ("end_time", "TEXT"), ("status", "TEXT")], plant.roster),
    ]
    plant_sql = [
        table("stations", [("station_id", "TEXT PRIMARY KEY"), ("name", "TEXT"), ("zone_id", "TEXT"), ("x", "INT"),
                           ("y", "INT"), ("kitted", "BOOLEAN"), ("buffer_min", "INT")], STATIONS),
        table("machines", [("machine_id", "TEXT PRIMARY KEY"), ("name", "TEXT"), ("kind", "TEXT"),
                           ("station_id", "TEXT"), ("zone_id", "TEXT"), ("x", "INT"), ("y", "INT"),
                           ("commissioned_year", "INT")],
              [(m[0], m[1], m[2], m[3], STATION[m[3]][2], STATION[m[3]][3], STATION[m[3]][4], m[4]) for m in MACHINES]),
        table("machine_telemetry", [("ts", "TIMESTAMP"), ("machine_id", "TEXT"), ("air_temp_k", "REAL"),
                                    ("process_temp_k", "REAL"), ("rpm", "INT"), ("torque_nm", "REAL"),
                                    ("tool_wear_min", "INT"), ("failure", "INT"), ("heat_dissipation_failure", "INT"),
                                    ("overstrain_failure", "INT"), ("source", "TEXT")], machine_telemetry),
        table("vibration_samples", VIBRATION_COLS,
              [[v[c] if c in v else VIBRATION_MACHINE for c, _ in VIBRATION_COLS] for v in plant.vibration]),
        table("weather_hourly", [("ts", "TIMESTAMP"), ("temperature_c", "REAL"), ("source", "TEXT")],
              [(ts, t, plant.weather_source) for ts, t in sorted(plant.temps.items())]),
        table("production_plan", [("shift_date", "DATE"), ("shift_code", "TEXT"), ("planned_units", "INT"),
                                  ("built_units", "INT"), ("downtime_min", "REAL"), ("incidents", "INT"),
                                  ("andon_pulls", "INT"), ("overtime", "BOOLEAN"), ("takt_s", "INT")], plan),
        table("units", [("unit_id", "TEXT PRIMARY KEY"), ("model", "TEXT"), ("body_style", "TEXT"),
                        ("variant", "TEXT"), ("colour", "TEXT"), ("shift_date", "DATE"), ("shift_code", "TEXT"),
                        ("completed_ts", "TIMESTAMP"), ("status", "TEXT"), ("defect_code", "TEXT")], units),
        table("work_orders", [("wo_id", "TEXT PRIMARY KEY"), ("created_ts", "TIMESTAMP"), ("asset_type", "TEXT"),
                              ("asset_id", "TEXT"), ("wo_type", "TEXT"), ("priority", "TEXT"), ("assigned_to", "TEXT"),
                              ("status", "TEXT"), ("closed_ts", "TIMESTAMP"), ("labour_min", "INT"),
                              ("incident_id", "TEXT"), ("notes", "TEXT")], work_orders),
    ]
    incident_cols = [
        ("incident_id", "TEXT PRIMARY KEY"), ("ts", "TIMESTAMP"), ("shift_code", "TEXT"), ("kind", "TEXT"),
        ("cause", "TEXT"), ("zone_id", "TEXT"), ("station_id", "TEXT"), ("machine_id", "TEXT"), ("agv_id", "TEXT"),
        ("x", "INT"), ("y", "INT"), ("severity", "TEXT"), ("reported_by", "TEXT"), ("worker_id", "TEXT"),
        ("response_s", "INT"), ("time_to_clear_s", "INT"), ("downtime_min", "REAL"), ("cars_lost", "INT"),
        ("cost_usd", "INT"), ("outcome", "TEXT"), ("false_alarm", "BOOLEAN"), ("supermarket_distance", "INT"),
        ("spill_kit_distance", "INT"), ("outdoor_temp_c", "REAL"), ("agv_utilisation_pct", "REAL"),
        ("osha_group", "TEXT"), ("recordable_injury", "BOOLEAN"), ("vibration_recording", "TEXT"),
        ("description", "TEXT")]
    ops = [
        table("zones", [("zone_id", "TEXT PRIMARY KEY"), ("name", "TEXT"), ("kind", "TEXT"), ("x0", "INT"),
                        ("y0", "INT"), ("x1", "INT"), ("y1", "INT")], ZONES),
        table("floor_cells", [("x", "INT"), ("y", "INT"), ("cell", "TEXT")],
              [(x, y, BLOCKED.get((x, y)) or zone_of(x, y)) for y in range(ROWS) for x in range(COLS)]),
        table("spill_kits", [("kit_id", "TEXT PRIMARY KEY"), ("x", "INT"), ("y", "INT"), ("zone_id", "TEXT")],
              [(k, x, y, zone_of(x, y)) for k, x, y in SPILL_KITS]),
        table("safety_rules", [("rule_id", "TEXT PRIMARY KEY"), ("cfr_ref", "TEXT"), ("summary", "TEXT"),
                               ("check_type", "TEXT"), ("params", "TEXT")], SAFETY_RULES),
        table("incidents", incident_cols, [[i[c] for c, _ in incident_cols] for i in incidents]),
        table("andon_events", [("andon_id", "TEXT PRIMARY KEY"), ("ts", "TIMESTAMP"), ("shift_code", "TEXT"),
                               ("station_id", "TEXT"), ("reason", "TEXT"), ("responder_id", "TEXT"),
                               ("response_s", "INT"), ("resolved_s", "INT"), ("stopped_line", "BOOLEAN"),
                               ("incident_id", "TEXT")], andon),
        table("data_sources", [("source", "TEXT PRIMARY KEY"), ("used_for", "TEXT"), ("status", "TEXT"),
                               ("licence", "TEXT"), ("credit", "TEXT")], [
            ("UCI AI4I 2020", "machine_telemetry", "ai4i2020" if (SEED / "ai4i2020.csv").exists() else "synthetic",
             "CC BY 4.0", "Matzka, S. (2020). AI4I 2020 Predictive Maintenance Dataset. UCI ML Repository. DOI 10.24432/C5HS5C"),
            ("OSHA Severe Injury Reports, NAICS 3361", "mix of safety event types", plant.osha_source,
             "Public domain", "U.S. Department of Labor, OSHA. Aggregated counts only; no endorsement implied"),
            ("BLS SOII, NAICS 3361", f"safety event frequency (TRC {plant.bls_rate} per 100 FTE)", plant.bls_source,
             "Public domain", "U.S. Bureau of Labor Statistics, Survey of Occupational Injuries and Illnesses"),
            ("Open-Meteo historical weather, Detroit MI", "outdoor temperature, machine heat", plant.weather_source,
             "CC BY 4.0", "Weather data by Open-Meteo.com"),
            ("Bosch CNC Machining dataset (UCI 752)", f"vibration_samples, evidence for {VIBRATION_MACHINE} torque alarms",
             "bosch-cnc" if plant.vibration else "missing", "CC BY 4.0",
             "Tnani, Feil, Diepold (2022), Procedia CIRP 107; github.com/boschresearch/CNC_Machining"),
            ("MovingAI MAPF benchmarks, warehouse maps", "data/seed/mapf for path-planner benchmarks",
             "static" if (SEED / "mapf").exists() else "missing", "Open Data Commons Attribution",
             "Stern et al. (2019), Multi-Agent Pathfinding: Definitions, Variants, and Benchmarks, SoCS; movingai.com"),
            ("OSHA 29 CFR 1910", "safety_rules (paraphrased)", "static", "Public domain", "U.S. Department of Labor"),
        ]),
        table("experiments", [("experiment_id", "TEXT PRIMARY KEY"), ("created_at", "TIMESTAMP"),
                              ("hypothesis", "TEXT"), ("policy", "TEXT"), ("baseline_mean_s", "REAL"),
                              ("treatment_mean_s", "REAL"), ("effect_pct", "REAL"), ("ci_low", "REAL"),
                              ("ci_high", "REAL"), ("status", "TEXT")], []),
    ]
    counts = {
        "fleet": {"agvs": len(plant.agvs), "agv_tasks": len(tasks), "agv_telemetry": len(agv_telemetry),
                  "fleet_hourly": len(plant.hourly)},
        "hr": {"workers": len(plant.workers), "certifications": len(plant.certs), "shifts": len(plant.roster)},
        "plant": {"stations": len(STATIONS), "machines": len(MACHINES), "machine_telemetry": len(machine_telemetry),
                  "vibration_samples": len(plant.vibration), "weather_hourly": len(plant.temps), "production_plan": len(plan), "units": len(units),
                  "work_orders": len(work_orders)},
        "ops": {"zones": len(ZONES), "floor_cells": ROWS * COLS, "spill_kits": len(SPILL_KITS),
                "safety_rules": len(SAFETY_RULES), "incidents": len(incidents), "andon_events": len(andon),
                "data_sources": 7, "experiments": 0},
    }
    print(f"anchors: weather={plant.weather_source}, osha={plant.osha_source}, bls={plant.bls_source} "
          f"(TRC {plant.bls_rate}); expected recordables {plant.expected_recordables:.2f}, "
          f"safety events ~{plant.safety_events:.0f}")
    return {"fleet": fleet, "hr": hr, "plant": plant_sql, "ops": ops}, counts


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    sql, counts = build()
    for name, parts in sql.items():
        path = OUT / f"{name}.sql"
        path.write_text("\n".join(parts) + "\n")
        detail = ", ".join(f"{t} {n:,}" for t, n in counts[name].items())
        print(f"wrote {path.relative_to(ROOT)} ({path.stat().st_size // 1024} KB): {detail}")


if __name__ == "__main__":
    main()
