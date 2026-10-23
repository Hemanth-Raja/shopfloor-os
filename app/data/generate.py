"""Generate the Shop-Floor OS datasets as SQL files, one per source database.

The site is a fictional car assembly plant: body shop, paint shop, trim line, powertrain
marriage, final assembly and end-of-line test, fed by AGVs from a parts supermarket and a
kitting area. 13 weeks of history with two production shifts (A 06-14, B 14-22) and a night
maintenance shift (N 22-06), plus a Saturday overtime shift in some weeks.

Standard library only. Deterministic (seed 42), so every run gives the same data.

    python -m app.data.generate            # writes data/sql/{fleet,hr,plant,ops}.sql

Sources:
  - Synthetic (ours): plant layout, stations, AGVs with their tasks and telemetry, workers,
    certifications, rosters, production, incidents, andon pulls, near misses, work orders.
  - UCI AI4I 2020 Predictive Maintenance (CC BY 4.0, DOI 10.24432/C5HS5C): telemetry for the
    ten plant machines when data/raw/ai4i2020.csv exists, otherwise synthesised with the same
    columns. Each machine's failure count drives how often it breaks down in the history.
  - OSHA 29 CFR 1910.22, .147, .178, .212 and .217 (U.S. Department of Labor, public domain):
    paraphrased rule summaries in SAFETY_RULES; verify wording against eCFR.

Patterns planted for the agents to find (all synthetic):
  - Fluid spills cluster at F30 fluid fill, far from the two spill kits (dock, maintenance bay).
  - Spills, obstructions and near misses peak around shift changes.
  - KIT-05 and CAR-03 are old and overdue for service; their faults rise over the quarter.
  - Body carriers cross the east pedestrian crossing too fast; near misses cluster there.
  - Part shortages spike on Monday mornings.
  - The night shift has fewer qualified responders, so incidents take longer to clear.
  - Some repairs were done by technicians whose lockout/tagout certification had lapsed.
"""

import csv
import math
import random
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "data" / "raw"
OUT = ROOT / "data" / "sql"
rng = random.Random(42)

ROWS, COLS = 30, 60            # grid cells; one cell is about 2 m
CELL_M = 2.0
START = datetime(2026, 7, 20)  # a Monday
DAYS = 91                      # 13 weeks
END = START + timedelta(days=DAYS)
TELEMETRY_DAYS = 7             # minute-level AGV and machine telemetry for the last week
TAKT_S = 90                    # one car every 90 s on the main line
SHIFTS = {"A": (6, 14), "B": (14, 22), "N": (22, 30)}  # N ends 06:00 next day
PRODUCTION_SHIFTS = ("A", "B")

# zone_id, name, kind, x0, y0, x1, y1 (inclusive grid cells; x = column, y = row).
# First match wins, so the pedestrian crossings are listed before the lanes they cross.
ZONES = [
    ("X-WEST", "West pedestrian crossing", "crossing", 12, 8, 13, 23),
    ("X-EAST", "East pedestrian crossing", "crossing", 46, 8, 47, 23),
    ("RECV", "Receiving dock", "dock", 0, 0, 9, 7),
    ("BODY", "Body shop", "body_shop", 14, 0, 29, 7),
    ("PAINT", "Paint shop", "paint_shop", 30, 0, 45, 7),
    ("MAINT", "Maintenance and rework bay", "maintenance", 48, 0, 59, 7),
    ("LANE-N", "North AGV lane", "agv_lane", 0, 8, 59, 9),
    ("SMKT", "Parts supermarket", "supermarket", 0, 10, 11, 29),
    ("TRIM", "Trim line", "line", 14, 10, 29, 17),
    ("MARR", "Marriage station", "line", 30, 10, 35, 17),
    ("FINAL", "Final assembly", "line", 36, 10, 45, 17),
    ("EOL", "End-of-line test", "line", 48, 10, 59, 17),
    ("PBS", "Painted body store", "buffer", 48, 18, 59, 20),
    ("LANE-S", "South AGV lane", "agv_lane", 14, 18, 47, 20),
    ("WALK", "Pedestrian walkway", "walkway", 14, 21, 59, 22),
    ("KIT", "Sequencing and kitting", "kitting", 14, 23, 33, 29),
    ("TEAM", "Team area and first aid", "amenities", 34, 23, 45, 29),
    ("CHG", "AGV charging", "charger", 48, 23, 59, 29),
]
ZONE_NAME = {z[0]: z[1] for z in ZONES}
RACKS = [(x, y) for y in (12, 15, 18, 21, 24, 27) for x in range(1, 11)]
CONVEYOR = [(x, 14) for x in range(14, 46)]
ROBOT_CELLS = [(x, y) for x in range(15, 29) for y in range(2, 6)]
PAINT_BOOTH = [(x, y) for x in range(31, 45) for y in range(2, 6)]
BLOCKED = {**{c: "rack" for c in RACKS}, **{c: "conveyor" for c in CONVEYOR},
           **{c: "robot_cell" for c in ROBOT_CELLS}, **{c: "paint_booth" for c in PAINT_BOOTH}}

# station_id, name, zone, x, y, kitted, buffer_min (minutes the line keeps running if supply stops)
STATIONS = [
    ("B10", "Underbody weld", "BODY", 16, 6, False, 10),
    ("B20", "Side frame weld", "BODY", 20, 6, False, 10),
    ("B30", "Roof brazing", "BODY", 24, 6, False, 10),
    ("B40", "Closures hemming", "BODY", 28, 6, False, 10),
    ("P10", "Pretreatment and e-coat", "PAINT", 32, 6, False, 15),
    ("P20", "Sealer", "PAINT", 36, 6, False, 15),
    ("P30", "Topcoat robots", "PAINT", 40, 6, False, 15),
    ("P40", "Paint inspection", "PAINT", 44, 6, False, 15),
    ("T10", "Wiring harness", "TRIM", 15, 13, True, 12),
    ("T20", "Headliner", "TRIM", 18, 13, True, 15),
    ("T30", "Instrument panel", "TRIM", 21, 13, True, 8),
    ("T40", "Glazing", "TRIM", 24, 13, True, 10),
    ("T50", "Door trim", "TRIM", 27, 13, True, 12),
    ("M10", "Powertrain marriage", "MARR", 32, 13, True, 6),
    ("F10", "Wheels and tyres", "FINAL", 36, 13, True, 10),
    ("F20", "Seats", "FINAL", 38, 13, True, 8),
    ("F30", "Fluid fill", "FINAL", 40, 13, False, 0),
    ("F40", "Bumpers and lamps", "FINAL", 42, 13, True, 12),
    ("F50", "Doors on", "FINAL", 44, 13, True, 10),
    ("E10", "Wheel alignment", "EOL", 50, 13, False, 0),
    ("E20", "Rolls and brake test", "EOL", 53, 13, False, 0),
    ("E30", "Water test", "EOL", 56, 13, False, 0),
    ("E40", "Final inspection", "EOL", 58, 13, False, 0),
]
STATION = {s[0]: s for s in STATIONS}
KITTED = [s for s in STATIONS if s[5]]
LINE_STATIONS = [s for s in STATIONS if s[2] in ("TRIM", "MARR", "FINAL", "EOL")]
SHORTAGE_WEIGHT = {"T30": 3, "M10": 2, "F20": 2}

# machine_id, name, kind, station_id, commissioned_year
MACHINES = [
    ("M-01", "Underbody weld robots", "weld_robot", "B10", 2014),
    ("M-02", "Side frame weld robots", "weld_robot", "B20", 2014),
    ("M-03", "Roof laser brazing cell", "weld_robot", "B30", 2019),
    ("M-04", "Hemming press", "press", "B40", 2011),
    ("M-05", "Topcoat paint robots", "paint_robot", "P30", 2016),
    ("M-06", "Main line conveyor", "conveyor", "T30", 2009),
    ("M-07", "Marriage lift", "lift", "M10", 2012),
    ("M-08", "Wheel nutrunner", "nutrunner", "F10", 2021),
    ("M-09", "Fluid fill rig", "fluid_fill", "F30", 2015),
    ("M-10", "Roll and brake tester", "test_rig", "E20", 2018),
]
FAULT_CAUSES = {
    "weld_robot": ["tip_dress_failure", "servo_overcurrent", "weld_spatter_sensor"],
    "press": ["die_misfeed", "hydraulic_pressure_low"],
    "paint_robot": ["atomiser_clog", "colour_change_fault"],
    "conveyor": ["chain_jam", "drive_overheat"],
    "lift": ["hydraulic_leak", "position_sensor_fault"],
    "nutrunner": ["torque_out_of_range", "controller_fault"],
    "fluid_fill": ["nozzle_leak", "vacuum_check_fail"],
    "test_rig": ["roller_bearing_overheat", "calibration_drift"],
}
REPAIR_S = {"weld_robot": 1100, "press": 1600, "paint_robot": 1300, "conveyor": 900, "lift": 1500,
            "nutrunner": 600, "fluid_fill": 800, "test_rig": 1000}

OLD_AGVS = ("KIT-05", "CAR-03")
AGV_FAULT_CODES = ["E12_WHEEL_SLIP", "E31_LIDAR_BLOCKED", "E40_BATTERY_LOW", "E52_NAV_LOST"]
SPILL_KITS = [("SK-1", 2, 7), ("SK-2", 54, 3)]  # where the spill kits are kept today

# All names are invented for the simulation.
FIRST = ["Aisha", "Ben", "Carla", "Dmitri", "Elena", "Farid", "Grace", "Hiro", "Ines", "Jamal", "Kofi",
         "Lena", "Marco", "Nadia", "Oscar", "Petra", "Quinn", "Rosa", "Sanjay", "Tara", "Umar", "Vera",
         "Wei", "Ximena", "Yusuf", "Zoe", "Anil", "Bianca", "Chidi", "Dana"]
LAST = ["Achterberg", "Banda", "Castillo", "Dubois", "Eriksen", "Fontaine", "Gallo", "Haddad", "Iversen",
        "Jovanovic", "Kowalski", "Lindqvist", "Moreau", "Novak", "Okonkwo", "Petrov", "Quintero", "Rahman",
        "Silva", "Tanaka", "Ulloa", "Varga", "Walsh", "Xu", "Yilmaz", "Zielinski"]
AREA_SKILLS = {"BODY": ["robot_cell_entry", "weld_inspection"], "PAINT": ["paint_inspection"],
               "TRIM": ["torque_tools", "manual_handling"], "MARR": ["torque_tools", "manual_handling"],
               "FINAL": ["torque_tools", "manual_handling"], "EOL": ["test_driving", "quality_inspection"]}
TEAM_LEADER_AREAS = [("BODY", ("BODY", "PAINT")), ("TRIM", ("TRIM", "MARR")), ("FINAL", ("FINAL",)), ("EOL", ("EOL",))]
TECH_SKILLS = ["machine_repair", "lockout_tagout", "electrical", "robot_cell_entry", "agv_reset"]
CERTS = {"forklift": 3, "lockout_tagout": 1, "robot_cell_entry": 2, "first_aid": 3, "press_operation": 1,
         "electrical": 3, "spill_response": 2}  # certification validity in years

MODELS = [("SF-S1", "sedan", 55), ("SF-X3", "suv", 35), ("SF-V2", "van", 10)]
VARIANTS = ["base", "comfort", "sport"]
COLOURS = ["arctic white", "graphite", "midnight blue", "signal red", "silver", "forest green"]

# Expected incidents per shift; production shifts are busier than the night shift.
RATES = {
    "A": {"part_shortage": 3.0, "equipment_fault": 2.2, "agv_fault": 1.3, "fluid_spill": 0.8,
          "obstruction": 1.0, "quality_defect": 1.4},
    "B": {"part_shortage": 2.8, "equipment_fault": 2.6, "agv_fault": 1.3, "fluid_spill": 0.8,
          "obstruction": 1.1, "quality_defect": 1.4},
    "N": {"equipment_fault": 0.5, "agv_fault": 0.5, "fluid_spill": 0.3, "obstruction": 0.2},
}
FALSE_ALARM = {"part_shortage": 0.03, "equipment_fault": 0.1, "agv_fault": 0.05, "fluid_spill": 0.06,
               "obstruction": 0.08, "quality_defect": 0.04}
RESPONDER_SKILL = {"part_shortage": "tugger_driving", "equipment_fault": "lockout_tagout", "agv_fault": "agv_reset",
                   "fluid_spill": "spill_response", "obstruction": "forklift", "quality_defect": "quality_inspection"}
ANDON_REASON = {"part_shortage": "missing_part", "equipment_fault": "equipment", "fluid_spill": "safety",
                "quality_defect": "quality_check", "agv_fault": "missing_part", "obstruction": "missing_part"}

# rule_id, reference, summary (paraphrased), check, params
SAFETY_RULES = [
    ("R-TRAIN", "29 CFR 1910.178(l)", "Only trained and evaluated operators may drive a powered industrial truck such as a forklift or tugger.",
     "requires_cert", "cert=forklift;applies_to=obstruction,part_shortage"),
    ("R-PED-CLEAR", "29 CFR 1910.178(m)(1)", "Do not drive a truck toward a person standing in front of a fixed object.",
     "min_separation", "cells=2"),
    ("R-CROSSING", "29 CFR 1910.178(n)(4)", "Slow down and sound the horn at cross aisles and where vision is obstructed.",
     "max_speed_at", "zones=X-EAST,X-WEST;speed_mps=0.5"),
    ("R-SAFE-SPEED", "29 CFR 1910.178(n)(8)", "Operate at a speed that allows a safe stop under the conditions.",
     "max_speed", "speed_mps=1.5"),
    ("R-SURFACE", "29 CFR 1910.22(a)(1)-(3)", "Keep walking-working surfaces clean, dry and free of hazards such as spills.",
     "zone_blocked_until_clear", "kinds=fluid_spill,obstruction"),
    ("R-LOTO", "29 CFR 1910.147", "Servicing or repair where unexpected start-up could cause injury needs lockout/tagout by an authorised employee.",
     "requires_cert", "cert=lockout_tagout;applies_to=equipment_fault"),
    ("R-GUARD", "29 CFR 1910.212(a)(1)", "Machine guarding must protect operators and others from point-of-operation hazards and moving parts.",
     "no_entry_while_running", "zones=BODY,PAINT"),
    ("R-PRESS", "29 CFR 1910.217", "Mechanical power presses need point-of-operation safeguarding; die setting and repair only by trained people under lockout.",
     "requires_cert", "cert=press_operation;applies_to=M-04"),
    ("R-ROBOT-CELL", "Plant rule (with 1910.212 and 1910.147)", "Enter a robot cell only with robot-cell entry certification and the cell locked out.",
     "requires_cert", "cert=robot_cell_entry;applies_to=BODY,PAINT"),
    ("R-CERT-VALID", "Plant rule", "A certification must be in date on the day the task is assigned.",
     "cert_valid", "on=assignment_date"),
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
    cells = [(x, y) for x in range(z[3], z[5] + 1) for y in range(z[4], z[6] + 1)
             if walkable(x, y) and zone_of(x, y) == zone_id]
    return rng.choice(cells)


def manhattan(a, b):
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def shift_window(day, code):
    h0, h1 = SHIFTS[code]
    start = datetime.combine(day, time()) + timedelta(hours=h0)
    return start, start + timedelta(hours=h1 - h0)


def event_time(start, end, shift_change_bias=0.0):
    """A moment in the shift; with some probability within 30 min of a shift change."""
    if rng.random() < shift_change_bias:
        edge = start if rng.random() < 0.5 else end - timedelta(minutes=30)
        return edge + timedelta(seconds=rng.randint(0, 1799))
    return start + timedelta(seconds=rng.randint(0, int((end - start).total_seconds()) - 1))


# ---------- people ----------

def make_workers():
    names = rng.sample([f"{f} {l}" for f in FIRST for l in LAST], 80)
    workers = []

    def add(role, shift, zone, skills, station=None, covers=()):
        x, y = (STATION[station][3], STATION[station][4]) if station else random_cell_in(zone)
        workers.append({"id": f"W-{len(workers) + 1:03d}", "name": names[len(workers)], "role": role,
                        "shift": shift, "zone": zone, "station": station, "skills": skills, "covers": covers,
                        "x": x, "y": y, "hired": (START - timedelta(days=rng.randint(60, 4000))).date()})

    for shift in PRODUCTION_SHIFTS:
        for st in STATIONS:
            skills = list(AREA_SKILLS[st[2]])
            if rng.random() < 0.2:
                skills.append("first_aid")
            if rng.random() < 0.15:
                skills.append("spill_response")
            add("line_operator", shift, st[2], skills, station=st[0])
        for area, covers in TEAM_LEADER_AREAS:
            add("team_leader", shift, area, ["andon_response", "first_aid", "spill_response", "agv_reset",
                                             AREA_SKILLS[area][0]], covers=covers)
        for i in range(3):
            add("maintenance_tech", shift, "MAINT", TECH_SKILLS + (["press_operation"] if i == 0 else []))
        for _ in range(2):
            add("quality_inspector", shift, "EOL", ["quality_inspection", "torque_audit"])
        for i in range(4):
            add("material_handler", shift, "SMKT",
                ["forklift", "tugger_driving", "spill_response"] + (["agv_reset"] if i % 2 == 0 else []))
        add("shift_supervisor", shift, "TEAM", ["incident_command", "first_aid", "lockout_tagout", "spill_response"])
    for _ in range(2):
        add("maintenance_tech", "N", "MAINT", list(TECH_SKILLS))
    for _ in range(2):
        add("material_handler", "N", "SMKT", ["forklift", "tugger_driving", "spill_response", "agv_reset"])
    add("shift_supervisor", "N", "TEAM", ["incident_command", "first_aid", "lockout_tagout"])
    return workers


def make_certs(workers):
    certs = []
    for w in workers:
        for skill in w["skills"]:
            if skill not in CERTS:
                continue
            if rng.random() < 0.06:
                expires = END.date() - timedelta(days=rng.randint(1, 80))
            else:
                expires = END.date() + timedelta(days=rng.randint(5, 365 * CERTS[skill]))
            certs.append([w["id"], skill, expires - timedelta(days=365 * CERTS[skill]), expires])
    # One night technician's lockout/tagout lapsed mid-quarter.
    night_tech = next(w for w in workers if w["shift"] == "N" and w["role"] == "maintenance_tech")
    for c in certs:
        if c[0] == night_tech["id"] and c[1] == "lockout_tagout":
            c[3] = START.date() + timedelta(days=50)
            c[2] = c[3] - timedelta(days=365)
    return certs


def make_roster(workers):
    rows, present = [], {}
    for d in range(DAYS):
        day = (START + timedelta(days=d)).date()
        if day.weekday() < 5:
            codes = ["A", "B", "N"]
        elif day.weekday() == 5 and rng.random() < 0.3:
            codes = ["A"]  # Saturday overtime
        else:
            codes = []
        for code in codes:
            h0, h1 = SHIFTS[code]
            here = []
            for w in workers:
                if w["shift"] != code:
                    continue
                if day.weekday() == 5:
                    status = "overtime" if rng.random() < 0.8 else "off"
                else:
                    r = rng.random()
                    status = "on" if r < 0.9 else "absent" if r < 0.95 else "leave"
                if status == "off":
                    continue
                rows.append((w["id"], day, code, f"{h0:02d}:00", f"{h1 % 24:02d}:00", status))
                if status in ("on", "overtime"):
                    here.append(w)
            present[(day, code)] = here
    return rows, present


# ---------- fleet ----------

def make_agvs():
    chargers = [(x, y) for y in range(24, 30, 2) for x in range(49, 60, 2)]
    specs = [("KIT", 8, "Kitting AGV K300", "kitting", 300, 1.0),
             ("TUG", 4, "Tugger T1500", "tugger", 1500, 1.2),
             ("CAR", 4, "Body carrier BC2000", "body_carrier", 2000, 0.9)]
    agvs = []
    for prefix, n, model, kind, payload, speed in specs:
        for k in range(1, n + 1):
            agv_id = f"{prefix}-{k:02d}"
            old = agv_id in OLD_AGVS
            year = 2017 if old else rng.randint(2020, 2025)
            service_age = rng.randint(140, 170) if old else rng.randint(5, 27)
            agvs.append({"id": agv_id, "model": model, "kind": kind, "payload": payload, "speed": speed,
                         "old": old, "commissioned": date(year, rng.randint(1, 12), 1),
                         "last_service": (END - timedelta(days=service_age)).date(),
                         "health": round(100 - (2026 - year) * 3.5 - rng.uniform(0, 4)),
                         "home": chargers[len(agvs) % len(chargers)],
                         "fault_weight": 1 + 0.15 * (2026 - year) + (2.5 if old else 0)})
    return agvs


def pick_agv(pool, d_index):
    weights = [a["fault_weight"] * (1 + d_index / DAYS if a["old"] else 1) for a in pool]
    return weighted(pool, weights)


def route(a):
    """Waypoints for one trip, following the AGV lanes and never crossing the conveyor."""
    if a["kind"] == "kitting":
        src_zone = "KIT" if rng.random() < 0.7 else "SMKT"
        sx, sy = random_cell_in(src_zone)
        st = rng.choice([s for s in KITTED if s[0] not in ("M10", "F10")])
        dx, dy = st[3], 15  # line-side racks south of the conveyor
        return "kit_delivery", src_zone, st[0], [(sx, sy), (sx, 19), (dx, 19), (dx, dy)], rng.randint(80, 300)
    if a["kind"] == "tugger":
        sx, sy = random_cell_in("RECV")
        if rng.random() < 0.6:
            dx, dy = random_cell_in("SMKT")
            dest = "SMKT"
        else:
            st = STATION[rng.choice(["M10", "F10"])]
            dx, dy, dest = st[3], 13, st[0]
        return "replenishment", "RECV", dest, [(sx, sy), (sx, 9), (dx, 9), (dx, dy)], rng.randint(400, 1500)
    if rng.random() < 0.5:  # painted body to store, through the east crossing
        dx, dy = random_cell_in("PBS")
        return "body_transfer", "PAINT", "PBS", [(44, 6), (44, 9), (48, 9), (48, dy), (dx, dy)], 1450
    sx, sy = random_cell_in("PBS")
    return "body_transfer", "PBS", "T10", [(sx, sy), (sx, 19), (14, 19), (14, 15), (15, 15)], 1450


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


def make_tasks(present, agvs, windows):
    tasks = []
    for (day, code) in present:
        if code not in PRODUCTION_SHIFTS:
            continue
        start, end = shift_window(day, code)
        for a in agvs:
            t = start + timedelta(seconds=rng.randint(0, 600))
            while t < end - timedelta(minutes=10):
                down = next((w for w in windows[a["id"]] if w[0] <= t < w[1]), None)
                if down:
                    t = down[1] + timedelta(seconds=rng.randint(60, 300))
                    continue
                kind, src, dest, points, payload = route(a)
                travel = path_len(points) * CELL_M / a["speed"]
                delay = rng.randint(120, 600) if rng.random() < 0.07 else 0
                finish = t + timedelta(seconds=int(2 * travel + rng.randint(60, 180) + delay))
                status = "late" if delay else "done"
                fault = next((w for w in windows[a["id"]] if t < w[0] < finish), None)
                if fault:
                    finish, status = fault[0], "aborted"
                tasks.append({"agv": a["id"], "type": kind, "shift": code, "from": src, "to": dest,
                              "start": t, "end": finish, "distance_m": int(2 * path_len(points) * CELL_M),
                              "payload": payload, "status": status, "delay": delay, "path": points})
                t = finish + timedelta(seconds=rng.randint(300, 1200))
    tasks.sort(key=lambda r: (r["start"], r["agv"]))
    for n, task in enumerate(tasks, 1):
        task["id"] = f"T-{n:06d}"
    return tasks


def make_agv_telemetry(agvs, tasks, windows):
    t0 = END - timedelta(days=TELEMETRY_DAYS)
    by_agv = defaultdict(list)
    for task in tasks:
        if task["end"] > t0:
            by_agv[task["agv"]].append(task)
    rows = []
    for a in agvs:
        mine, faults = by_agv[a["id"]], [w for w in windows[a["id"]] if w[1] > t0]
        battery, charging, i = rng.uniform(70, 95), False, 0
        drain = (0.18 if a["kind"] == "body_carrier" else 0.12) * 100 / a["health"]
        state = "idle"
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
                    if zone_of(x, y) in ("X-EAST", "X-WEST"):
                        speed = rng.uniform(0.9, 1.3) if a["kind"] == "body_carrier" else rng.uniform(0.3, 0.5)
                    elif a["kind"] == "tugger" and rng.random() < 0.02:
                        speed = rng.uniform(1.6, 1.8)
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


# ---------- plant ----------

def ai4i_rows():
    path = RAW / "ai4i2020.csv"
    if path.exists():
        with path.open(newline="", encoding="utf-8-sig") as f:
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


def make_machine_telemetry():
    """1,000 AI4I rows per machine, one every 10 minutes over the last week. Returns rows and
    per-machine breakdown weights (more AI4I failures and an older machine break down more)."""
    source = list(ai4i_rows())
    t0 = END - timedelta(days=TELEMETRY_DAYS)
    rows, weights = [], []
    for i, m in enumerate(MACHINES):
        chunk = [source[(i * 1000 + k) % len(source)] for k in range(1000)]
        rows += [(t0 + timedelta(minutes=10 * k), m[0], *r[:8], r[9]) for k, r in enumerate(chunk)]
        weights.append((sum(r[5] for r in chunk) + 5) * (1 + (2026 - m[4]) / 10))
    return rows, weights


# ---------- incidents and everything that follows from them ----------

def has_skill(w, skill, day, cert_expiry, check_cert=True):
    if skill not in w["skills"]:
        return False
    return not (check_cert and skill in CERTS) or cert_expiry[(w["id"], skill)] >= day


def pick_responder(crew, skill, x, y, day, cert_expiry, check_cert=True):
    able = sorted((w for w in crew if has_skill(w, skill, day, cert_expiry, check_cert)),
                  key=lambda w: manhattan((w["x"], w["y"]), (x, y)))
    if not able:
        return None
    return able[1] if len(able) > 1 and rng.random() < 0.25 else able[0]  # nearest is sometimes busy


def make_incident(kind, ts, code, crew, agvs, machine_weights, d_index, cert_expiry):
    station = machine = agv = None
    zone = None
    if kind == "part_shortage":
        station = weighted([s[0] for s in KITTED], [SHORTAGE_WEIGHT.get(s[0], 1) for s in KITTED])
        cause = weighted(["agv_late", "mispick", "supplier_short", "agv_fault"], [4, 2, 2, 1])
        if cause in ("agv_late", "agv_fault"):
            pool = [a for a in agvs if a["kind"] == ("tugger" if station in ("M10", "F10") else "kitting")]
            agv = pick_agv(pool, d_index)
    elif kind == "equipment_fault":
        m = weighted(MACHINES, machine_weights)
        machine, station, cause = m[0], m[3], rng.choice(FAULT_CAUSES[m[2]])
    elif kind == "agv_fault":
        agv = pick_agv(agvs, d_index)
        cause = weighted(AGV_FAULT_CODES, [3, 2, 4 if agv["old"] else 1, 2])
        zone = weighted(["LANE-N", "LANE-S", "X-EAST", "X-WEST", "KIT", "PBS"], [3, 3, 1, 1, 2, 1])
    elif kind == "fluid_spill":
        zone = weighted(["FINAL", "MARR", "EOL", "MAINT", "TRIM", "LANE-S"], [6, 2, 1, 1, 1, 1])
        if zone == "FINAL" and rng.random() < 0.7:
            station = "F30"
            cause = weighted(["coolant", "brake_fluid", "washer_fluid"], [4, 3, 2])
        else:
            cause = weighted(["hydraulic_oil", "gear_oil", "coolant"], [3, 2, 1])
    elif kind == "obstruction":
        zone = weighted(["LANE-S", "LANE-N", "X-EAST", "X-WEST", "KIT"], [4, 2, 2, 1, 1])
        cause = rng.choice(["dunnage", "parked_tugger", "pallet", "empty_rack"])
        agv = rng.choice(agvs)
    else:  # quality_defect
        station = rng.choice(LINE_STATIONS)[0]
        cause = rng.choice(["torque_out_of_spec", "paint_run", "missing_clip", "seal_gap", "connector_not_seated"])

    if station:
        st = STATION[station]
        zone, x, y = st[2], st[3] + (rng.randint(-1, 1) if kind == "fluid_spill" else 0), st[4]
    else:
        x, y = random_cell_in(zone)

    # Repairs and forklift jobs sometimes went to whoever was free without checking certifications,
    # half the time on the thinly staffed night shift.
    check_cert = not (kind in ("equipment_fault", "obstruction") and rng.random() < (0.5 if code == "N" else 0.15))
    responder = pick_responder(crew, RESPONDER_SKILL[kind], x, y, ts.date(), cert_expiry, check_cert)
    penalty = 0
    if responder is None:  # nobody qualified on shift: the supervisor handles it and calls someone in
        responder = next((w for w in crew if w["role"] == "shift_supervisor"), crew[0] if crew else None)
        penalty = 600
    walk_s = manhattan((responder["x"], responder["y"]), (x, y)) * CELL_M / 1.2 if responder else 300
    response_s = int(walk_s + rng.randint(20, 90) + penalty / 2)

    kit_distance = min(manhattan((kx, ky), (x, y)) for _, kx, ky in SPILL_KITS) if kind == "fluid_spill" else None
    if kind == "part_shortage":
        clear = {"agv_late": 300, "mispick": 420, "supplier_short": 1500, "agv_fault": 540}[cause] + walk_s
    elif kind == "equipment_fault":
        mk = next(m for m in MACHINES if m[0] == machine)[2]
        clear = REPAIR_S[mk] + walk_s + (0 if responder and responder["role"] == "maintenance_tech" else 900)
    elif kind == "agv_fault":
        clear = 360 + walk_s + (600 if cause == "E40_BATTERY_LOW" and agv["old"] else 0)
    elif kind == "fluid_spill":
        clear = 240 + kit_distance * CELL_M / 1.2 * 2 + walk_s
    elif kind == "obstruction":
        clear = 300 + walk_s
    else:
        clear = 600
    clear = max(60, clear + penalty + rng.gauss(0, 0.15 * clear))
    if code == "N":
        clear *= 1.3

    false_alarm = rng.random() < FALSE_ALARM[kind]
    if false_alarm:
        clear, line_stop = rng.randint(60, 240), 0
    elif code == "N":
        line_stop = 0
    elif kind == "part_shortage":
        line_stop = max(0, clear - STATION[station][6] * 60 * rng.uniform(0.3, 1.0))
    elif kind == "equipment_fault":
        bank = STATION[station][6] * 60 if zone in ("BODY", "PAINT") else 0
        line_stop = max(0, clear - bank) * (0.3 if zone == "EOL" else 1)  # EOL cars can wait off-line
    elif kind == "agv_fault":
        stops = rng.random() < (0.6 if agv["kind"] == "body_carrier" else 0.3)
        line_stop = max(0, clear - rng.uniform(150, 450)) if stops else 0
    elif kind == "fluid_spill":
        line_stop = clear * 0.6 if station and rng.random() < 0.6 else 0
    elif kind == "obstruction":
        line_stop = max(0, clear - 240) if rng.random() < 0.3 else 0
    else:
        line_stop = 300 if rng.random() < 0.1 else 0
    clear, line_stop = int(clear), int(line_stop)

    severity = ("low" if false_alarm else "high" if line_stop >= 900 or clear >= 2400
                else "medium" if line_stop > 0 else "low")
    outcome = ("false_alarm" if false_alarm else "rework" if kind == "quality_defect"
               else "escalated" if clear > 3600 else "resolved")
    operators = [w for w in crew if station and w["station"] == station]
    reporter = operators[0] if operators else (rng.choice(crew) if crew else None)
    agv_id = agv["id"] if agv else None
    sname = STATION[station][1] if station else None
    pretty = cause.replace("_", " ")
    description = {
        "part_shortage": f"Parts shortage at {station} {sname}: {pretty}" + (f" ({agv_id})" if agv_id else ""),
        "equipment_fault": f"{machine} at {station} {sname} stopped: {pretty}",
        "agv_fault": f"{agv_id} stopped in {ZONE_NAME[zone]} with {cause}",
        "fluid_spill": f"{pretty.capitalize()} spill at {station + ' ' + sname if station else ZONE_NAME[zone]}; area cordoned",
        "obstruction": f"{pretty.capitalize()} blocking {ZONE_NAME[zone]}; {agv_id} held",
        "quality_defect": f"{pretty.capitalize()} found at {station} {sname}; unit to rework",
    }[kind]
    return {"ts": ts, "shift_code": code, "kind": kind, "cause": cause, "zone_id": zone, "station_id": station,
            "machine_id": machine, "agv_id": agv_id, "x": x, "y": y, "severity": severity,
            "reported_by": reporter["id"] if reporter else None, "worker_id": responder["id"] if responder else None,
            "response_s": response_s, "time_to_clear_s": clear, "line_stop_s": line_stop,
            "cars_lost": round(line_stop / TAKT_S), "outcome": outcome, "false_alarm": false_alarm,
            "spill_kit_distance": kit_distance, "description": description}


def make_incidents(present, agvs, machine_weights, cert_expiry):
    incidents = []
    for (day, code), crew in present.items():
        start, end = shift_window(day, code)
        d_index = (day - START.date()).days
        for kind, lam in RATES[code].items():
            if kind == "part_shortage" and day.weekday() == 0 and code == "A":
                lam *= 1.8  # supplier deliveries after the weekend
            for _ in range(poisson(lam)):
                ts = event_time(start, end, 0.45 if kind in ("fluid_spill", "obstruction") else 0.0)
                inc = make_incident(kind, ts, code, crew, agvs, machine_weights, d_index, cert_expiry)
                inc["shift_key"] = (day, code)
                incidents.append(inc)
    incidents.sort(key=lambda r: r["ts"])
    windows = defaultdict(list)  # agv_id -> [(start, end, fault_code, (x, y))]
    for n, inc in enumerate(incidents, 1):
        inc["incident_id"] = f"INC-{n:05d}"
        stuck = inc["kind"] == "agv_fault" or (inc["kind"] == "part_shortage" and inc["cause"] == "agv_fault")
        if stuck and not inc["false_alarm"]:
            code = inc["cause"] if inc["kind"] == "agv_fault" else "E52_NAV_LOST"
            windows[inc["agv_id"]].append((inc["ts"], inc["ts"] + timedelta(seconds=inc["time_to_clear_s"]),
                                           code, (inc["x"], inc["y"])))
    return incidents, windows


def make_andon(present, incidents):
    rows = []
    for (day, code), crew in present.items():
        if code not in PRODUCTION_SHIFTS:
            continue
        start, _ = shift_window(day, code)
        leaders = {area: w for w in crew if w["role"] == "team_leader" for area in w["covers"]}
        supervisor = next((w for w in crew if w["role"] == "shift_supervisor"), None)
        for st in STATIONS:
            for _ in range(poisson(0.9)):
                leader = leaders.get(st[2]) or supervisor
                response = max(8, int(rng.gauss(45, 18))) + (0 if st[2] in leaders else 60)
                stopped = rng.random() < 0.03
                resolved = response + max(20, int(rng.gauss(110, 50))) + (rng.randint(90, 300) if stopped else 0)
                reason = weighted(["help_needed", "missing_part", "quality_check", "tool_issue", "safety"],
                                  [4, 2, 3, 2, 0.5])
                rows.append([start + timedelta(seconds=rng.randint(0, 8 * 3600 - 1)), code, st[0], reason,
                             leader["id"] if leader else None, response, resolved, stopped, None])
    for inc in incidents:
        if inc["station_id"] and inc["shift_code"] in PRODUCTION_SHIFTS:
            rows.append([inc["ts"], inc["shift_code"], inc["station_id"], ANDON_REASON[inc["kind"]], inc["worker_id"],
                         inc["response_s"], inc["time_to_clear_s"], inc["line_stop_s"] > 0, inc["incident_id"]])
    rows.sort(key=lambda r: r[0])
    return [[f"AND-{n:05d}"] + r for n, r in enumerate(rows, 1)]


def make_near_misses(present, agvs):
    rows = []
    for (day, code), crew in present.items():
        start, end = shift_window(day, code)
        for _ in range(poisson(0.3 if code == "N" else 2.0)):
            ts = event_time(start, end, 0.4)
            kind = weighted(["agv_pedestrian", "tugger_pedestrian", "robot_cell_entry", "slip_no_fall",
                             "falling_object", "forklift_reversing"], [5, 2, 1, 2, 1, 1])
            agv = None
            if kind == "agv_pedestrian":
                zone = weighted(["X-EAST", "X-WEST", "LANE-S", "LANE-N"], [6, 2, 1, 1])
                agv = weighted(agvs, [4 if a["kind"] == "body_carrier" and zone == "X-EAST" else 1 for a in agvs])
                text = f"{agv['id']} came within 1 m of a pedestrian in {ZONE_NAME[zone]}"
            elif kind == "tugger_pedestrian":
                zone = weighted(["X-WEST", "LANE-N", "SMKT"], [3, 2, 2])
                agv = rng.choice([a for a in agvs if a["kind"] == "tugger"])
                text = f"Tugger {agv['id']} passed close to a pedestrian in {ZONE_NAME[zone]}"
            elif kind == "robot_cell_entry":
                zone, text = "BODY", "Operator reached through a robot cell guard opening while the cell was in auto"
            elif kind == "slip_no_fall":
                zone = weighted(["FINAL", "MARR", "EOL"], [4, 1, 1])
                text = f"Slipped on fluid residue in {ZONE_NAME[zone]}, no fall"
            elif kind == "falling_object":
                zone, text = "SMKT", "Part fell from an upper rack level in the supermarket"
            else:
                zone = weighted(["RECV", "SMKT"], [3, 2])
                text = f"Forklift reversed without a spotter near pedestrians in {ZONE_NAME[zone]}"
            x, y = random_cell_in(zone)
            rows.append([ts, code, kind, zone, x, y, agv["id"] if agv else None,
                         rng.choice(crew)["id"] if crew else None, text])
    rows.sort(key=lambda r: r[0])
    return [[f"NM-{n:04d}"] + r for n, r in enumerate(rows, 1)]


def make_work_orders(incidents, agvs, workers):
    rows = []
    for inc in incidents:
        if inc["kind"] in ("equipment_fault", "agv_fault") and not inc["false_alarm"]:
            closed = inc["ts"] + timedelta(seconds=inc["time_to_clear_s"] + rng.randint(300, 3600))
            done = closed < END
            rows.append([inc["ts"], "machine" if inc["machine_id"] else "agv", inc["machine_id"] or inc["agv_id"],
                         "reactive", "high" if inc["severity"] == "high" else "medium", inc["worker_id"],
                         "closed" if done else "open", closed if done else None,
                         inc["time_to_clear_s"] // 60 + rng.randint(10, 60), inc["incident_id"], inc["cause"]])
    techs = [w["id"] for w in workers if w["role"] == "maintenance_tech" and w["shift"] == "N"]
    for m in MACHINES:
        t = END - timedelta(days=rng.randint(1, 14))
        while t > START:
            when = datetime.combine(t.date(), time(23, 0))
            labour = rng.randint(60, 180)
            rows.append([when, "machine", m[0], "planned", "low", rng.choice(techs), "closed",
                         when + timedelta(minutes=labour), labour, None, "preventive maintenance"])
            t -= timedelta(days=14)
    for a in agvs:
        last = datetime.combine(a["last_service"], time(23, 0))
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


def make_production(present, incidents, andon):
    stops, counts, quality = defaultdict(int), defaultdict(int), defaultdict(list)
    for inc in incidents:
        stops[inc["shift_key"]] += inc["line_stop_s"]
        counts[inc["shift_key"]] += 1
        if inc["kind"] == "quality_defect" and not inc["false_alarm"]:
            quality[inc["shift_key"]].append(inc)
    pulls = defaultdict(int)
    for a in andon:
        day = a[1].date()
        key = (day, a[2])
        pulls[key] += 1
        if a[8] and a[9] is None:  # routine andon pulls that stopped the line
            stops[key] += a[7]
    plan, units = [], []
    planned = int(7.5 * 3600 // TAKT_S)
    for key in present:
        day, code = key
        if code not in PRODUCTION_SHIFTS:
            continue
        start, _ = shift_window(day, code)
        built = max(0, planned - round(stops[key] / TAKT_S) - rng.randint(0, 4))
        plan.append((day, code, planned, built, stops[key], counts[key], pulls[key], day.weekday() == 5))
        step = 7.5 * 3600 / max(built, 1)
        shift_units = []
        for i in range(built):
            model = weighted(MODELS, [m[2] for m in MODELS])
            hold = rng.random() < 0.015
            shift_units.append([f"SF{day:%y%m%d}{code}{i + 1:03d}", model[0], model[1], rng.choice(VARIANTS),
                                rng.choice(COLOURS), day, code,
                                start + timedelta(minutes=30, seconds=int(i * step)),
                                "hold" if hold else "shipped", "audit_hold" if hold else None])
        for inc in quality[key]:
            unit = next((u for u in shift_units if u[7] >= inc["ts"]), shift_units[-1] if shift_units else None)
            if unit:
                unit[8], unit[9] = "rework", inc["cause"]
        units += shift_units
    return plan, units


# ---------- SQL ----------

def build():
    workers = make_workers()
    certs = make_certs(workers)
    cert_expiry = {(c[0], c[1]): c[3] for c in certs}
    roster, present = make_roster(workers)
    agvs = make_agvs()
    machine_telemetry, machine_weights = make_machine_telemetry()
    incidents, windows = make_incidents(present, agvs, machine_weights, cert_expiry)
    andon = make_andon(present, incidents)
    near_misses = make_near_misses(present, agvs)
    work_orders = make_work_orders(incidents, agvs, workers)
    plan, units = make_production(present, incidents, andon)
    tasks = make_tasks(present, agvs, windows)
    agv_telemetry = make_agv_telemetry(agvs, tasks, windows)
    faults = defaultdict(int)
    for inc in incidents:
        if inc["kind"] == "agv_fault" and not inc["false_alarm"]:
            faults[inc["agv_id"]] += 1

    fleet = [
        table("agvs", [("agv_id", "TEXT PRIMARY KEY"), ("model", "TEXT"), ("kind", "TEXT"), ("home_x", "INT"),
                       ("home_y", "INT"), ("battery_pct", "INT"), ("battery_health_pct", "INT"), ("status", "TEXT"),
                       ("max_payload_kg", "INT"), ("top_speed_mps", "REAL"), ("commissioned_date", "DATE"),
                       ("last_service_date", "DATE"), ("faults_90d", "INT")],
              [(a["id"], a["model"], a["kind"], *a["home"], a["battery_now"], a["health"], a["state_now"],
                a["payload"], a["speed"], a["commissioned"], a["last_service"], faults[a["id"]]) for a in agvs]),
        table("agv_tasks", [("task_id", "TEXT PRIMARY KEY"), ("agv_id", "TEXT"), ("task_type", "TEXT"),
                            ("shift_code", "TEXT"), ("from_zone", "TEXT"), ("to_location", "TEXT"),
                            ("start_ts", "TIMESTAMP"), ("end_ts", "TIMESTAMP"), ("distance_m", "INT"),
                            ("payload_kg", "INT"), ("status", "TEXT"), ("delay_s", "INT")],
              [(t["id"], t["agv"], t["type"], t["shift"], t["from"], t["to"], t["start"], t["end"],
                t["distance_m"], t["payload"], t["status"], t["delay"]) for t in tasks]),
        table("agv_telemetry", [("ts", "TIMESTAMP"), ("agv_id", "TEXT"), ("x", "INT"), ("y", "INT"),
                                ("speed_mps", "REAL"), ("battery_pct", "REAL"), ("state", "TEXT"),
                                ("fault_code", "TEXT"), ("task_id", "TEXT")], agv_telemetry),
    ]
    hr = [
        table("workers", [("worker_id", "TEXT PRIMARY KEY"), ("name", "TEXT"), ("role", "TEXT"), ("shift_code", "TEXT"),
                          ("station_id", "TEXT"), ("home_zone", "TEXT"), ("skills", "TEXT"),
                          ("forklift_licence", "BOOLEAN"), ("x", "INT"), ("y", "INT"), ("hire_date", "DATE")],
              [(w["id"], w["name"], w["role"], w["shift"], w["station"], w["zone"], ",".join(w["skills"]),
                "forklift" in w["skills"], w["x"], w["y"], w["hired"]) for w in workers]),
        table("certifications", [("worker_id", "TEXT"), ("cert", "TEXT"), ("issued_date", "DATE"),
                                 ("expires_date", "DATE")], certs),
        table("shifts", [("worker_id", "TEXT"), ("shift_date", "DATE"), ("shift_code", "TEXT"),
                         ("start_time", "TEXT"), ("end_time", "TEXT"), ("status", "TEXT")], roster),
    ]
    plant = [
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
        table("production_plan", [("shift_date", "DATE"), ("shift_code", "TEXT"), ("planned_units", "INT"),
                                  ("built_units", "INT"), ("line_stop_s", "INT"), ("incidents", "INT"),
                                  ("andon_pulls", "INT"), ("overtime", "BOOLEAN")], plan),
        table("units", [("unit_id", "TEXT PRIMARY KEY"), ("model", "TEXT"), ("body_style", "TEXT"),
                        ("variant", "TEXT"), ("colour", "TEXT"), ("shift_date", "DATE"), ("shift_code", "TEXT"),
                        ("completed_ts", "TIMESTAMP"), ("status", "TEXT"), ("defect_code", "TEXT")], units),
        table("work_orders", [("wo_id", "TEXT PRIMARY KEY"), ("created_ts", "TIMESTAMP"), ("asset_type", "TEXT"),
                              ("asset_id", "TEXT"), ("wo_type", "TEXT"), ("priority", "TEXT"), ("assigned_to", "TEXT"),
                              ("status", "TEXT"), ("closed_ts", "TIMESTAMP"), ("labour_min", "INT"),
                              ("incident_id", "TEXT"), ("notes", "TEXT")], work_orders),
    ]
    incident_cols = ["incident_id", "ts", "shift_code", "kind", "cause", "zone_id", "station_id", "machine_id",
                     "agv_id", "x", "y", "severity", "reported_by", "worker_id", "response_s", "time_to_clear_s",
                     "line_stop_s", "cars_lost", "outcome", "false_alarm", "spill_kit_distance", "description"]
    incident_types = {"ts": "TIMESTAMP", "x": "INT", "y": "INT", "response_s": "INT", "time_to_clear_s": "INT",
                      "line_stop_s": "INT", "cars_lost": "INT", "false_alarm": "BOOLEAN", "spill_kit_distance": "INT"}
    ops = [
        table("zones", [("zone_id", "TEXT PRIMARY KEY"), ("name", "TEXT"), ("kind", "TEXT"), ("x0", "INT"),
                        ("y0", "INT"), ("x1", "INT"), ("y1", "INT")], ZONES),
        table("floor_cells", [("x", "INT"), ("y", "INT"), ("cell", "TEXT")],
              [(x, y, BLOCKED.get((x, y)) or zone_of(x, y)) for y in range(ROWS) for x in range(COLS)]),
        table("spill_kits", [("kit_id", "TEXT PRIMARY KEY"), ("x", "INT"), ("y", "INT"), ("zone_id", "TEXT")],
              [(k, x, y, zone_of(x, y)) for k, x, y in SPILL_KITS]),
        table("safety_rules", [("rule_id", "TEXT PRIMARY KEY"), ("cfr_ref", "TEXT"), ("summary", "TEXT"),
                               ("check_type", "TEXT"), ("params", "TEXT")], SAFETY_RULES),
        table("incidents", [(c, "TEXT PRIMARY KEY" if c == "incident_id" else incident_types.get(c, "TEXT"))
                            for c in incident_cols], [[i[c] for c in incident_cols] for i in incidents]),
        table("andon_events", [("andon_id", "TEXT PRIMARY KEY"), ("ts", "TIMESTAMP"), ("shift_code", "TEXT"),
                               ("station_id", "TEXT"), ("reason", "TEXT"), ("responder_id", "TEXT"),
                               ("response_s", "INT"), ("resolved_s", "INT"), ("stopped_line", "BOOLEAN"),
                               ("incident_id", "TEXT")], andon),
        table("near_misses", [("near_miss_id", "TEXT PRIMARY KEY"), ("ts", "TIMESTAMP"), ("shift_code", "TEXT"),
                              ("kind", "TEXT"), ("zone_id", "TEXT"), ("x", "INT"), ("y", "INT"), ("agv_id", "TEXT"),
                              ("reported_by", "TEXT"), ("description", "TEXT")], near_misses),
        table("experiments", [("experiment_id", "TEXT PRIMARY KEY"), ("created_at", "TIMESTAMP"),
                              ("hypothesis", "TEXT"), ("policy", "TEXT"), ("baseline_mean_s", "REAL"),
                              ("treatment_mean_s", "REAL"), ("effect_pct", "REAL"), ("ci_low", "REAL"),
                              ("ci_high", "REAL"), ("status", "TEXT")], []),
    ]
    counts = {
        "fleet": {"agvs": len(agvs), "agv_tasks": len(tasks), "agv_telemetry": len(agv_telemetry)},
        "hr": {"workers": len(workers), "certifications": len(certs), "shifts": len(roster)},
        "plant": {"stations": len(STATIONS), "machines": len(MACHINES), "machine_telemetry": len(machine_telemetry),
                  "production_plan": len(plan), "units": len(units), "work_orders": len(work_orders)},
        "ops": {"zones": len(ZONES), "floor_cells": ROWS * COLS, "spill_kits": len(SPILL_KITS),
                "safety_rules": len(SAFETY_RULES), "incidents": len(incidents), "andon_events": len(andon),
                "near_misses": len(near_misses), "experiments": 0},
    }
    return {"fleet": fleet, "hr": hr, "plant": plant, "ops": ops}, counts


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
