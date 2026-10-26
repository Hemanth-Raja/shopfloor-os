"""The zone read without Zetaris: the rows of ZONE_SQL (app/engines/shopfloor_world.py) built by hand from the four
Postgres databases, for a side-by-side with the one federated query.

    .venv/bin/python compare/hand-coded/zone_read.py FINAL        # hand-coded vs Zetaris, rows compared, timed

Postgres cannot join across databases, so this sends one query per table to its own database (through `docker exec
shopfloor-pg psql`, read-only, no driver) and joins the rows in Python. Everything between the two markers below is
the hand-coded read that Zetaris replaces; the rest is the comparison harness.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# --- hand-coded read: start ---
def psql(db: str, sql: str) -> list[list[str]]:
    """One read-only statement on one database; rows as strings ('' for NULL)."""
    out = subprocess.run(["docker", "exec", "-e", "PGOPTIONS=-c default_transaction_read_only=on", "shopfloor-pg",
                          "psql", "-U", "postgres", "-d", db, "-At", "-F", "\t", "-v", "ON_ERROR_STOP=1", "-c", sql],
                         capture_output=True, text=True, check=True, timeout=60).stdout
    return [line.split("\t") for line in out.splitlines() if line]


def quoted(ids) -> str:
    return ", ".join("'" + i.replace("'", "''") + "'" for i in sorted({i for i in ids if i})) or "NULL"


def fetch(zone: str, limit: int, run=psql) -> dict:
    """Seven queries over four databases: the incidents first, then only the rows they point to."""
    inc = run("ops", "SELECT incident_id, ts, shift_code, kind, cause, zone_id, station_id, machine_id, agv_id, severity, "
                     "time_to_clear_s, downtime_min, cars_lost, cost_usd, outcome, false_alarm, description, worker_id, "
                     f"vibration_recording FROM incidents WHERE zone_id = '{zone}' ORDER BY ts DESC LIMIT {int(limit)}")
    col = lambda i: [r[i] for r in inc]  # noqa: E731
    return {
        "incidents": inc,
        "stations": run("plant", f"SELECT station_id, name FROM stations WHERE station_id IN ({quoted(col(6))})"),
        "machines": run("plant", f"SELECT machine_id, name FROM machines WHERE machine_id IN ({quoted(col(7))})"),
        "recordings": run("plant", "SELECT recording, label, x_rms FROM vibration_samples "
                                   f"WHERE recording IN ({quoted(col(18))})"),
        "workers": run("hr", f"SELECT worker_id, name, role, skills FROM workers WHERE worker_id IN ({quoted(col(17))})"),
        "loto": run("hr", "SELECT worker_id, expires_date FROM certifications WHERE cert = 'lockout_tagout' "
                          f"AND worker_id IN ({quoted(col(17))})"),
        "agvs": run("fleet", "SELECT agv_id, model, battery_pct, faults_90d, last_service_date FROM agvs "
                             f"WHERE agv_id IN ({quoted(col(8))})"),
    }


def join(t: dict) -> list[list[str]]:
    """The LEFT JOINs of ZONE_SQL, in Python, in its column order."""
    by = lambda rows: {r[0]: r for r in rows}  # noqa: E731
    st, ma, rec, wk, lo, ag = (by(t[k]) for k in ("stations", "machines", "recordings", "workers", "loto", "agvs"))
    out = []
    for (iid, ts, shift, kind, cause, zone, sid, mid, aid, sev, ttc, down, cars, cost, outcome, fa, desc, wid,
         vrec) in t["incidents"]:
        s, m, w, c, a, v = (st.get(sid), ma.get(mid), wk.get(wid), lo.get(wid), ag.get(aid), rec.get(vrec))
        out.append([iid, ts, shift, kind, cause, zone, sid, s[1] if s else "", mid, m[1] if m else "", aid, sev, ttc,
                    down, cars, cost, outcome, fa, desc, wid, w[1] if w else "", w[2] if w else "", w[3] if w else "",
                    c[1] if c else "", a[1] if a else "", a[2] if a else "", a[3] if a else "", a[4] if a else "",
                    vrec, v[1] if v else "", v[2] if v else ""])
    return out
# --- hand-coded read: end ---


def norm(v) -> str:
    """Compare values, not formats: Zetaris and psql print timestamps, booleans and numbers differently."""
    s = "" if v is None else str(v)
    if s in ("t", "true"):
        return "true"
    if s in ("f", "false"):
        return "false"
    if len(s) >= 19 and s[4] == "-" and s[10] in " T":
        return s[:19].replace("T", " ")
    try:
        return repr(round(float(s), 6))
    except ValueError:
        return s


def counted_lines() -> int:
    """Non-blank, non-comment lines between the markers: the code a team would write and maintain."""
    text = Path(__file__).read_text().split("# --- hand-coded read: start ---")[1].split("# --- hand-coded read: end ---")[0]
    return sum(1 for line in text.splitlines()
               if line.strip() and not line.strip().startswith(("#", '"""')) and not line.strip().endswith('"""'))


def main(zone: str = "FINAL", limit: int = 8) -> None:
    from app.engines.shopfloor_world import ZONE_SQL, zone_query
    from app.zetaris import run_sql
    t = time.perf_counter()
    hand = join(fetch(zone, limit))
    hand_s = time.perf_counter() - t
    t = time.perf_counter()
    fed = run_sql(zone_query(zone, limit))
    fed_s = time.perf_counter() - t
    same = [[norm(v) for v in r] for r in hand] == [[norm(v) for v in r] for r in fed]
    sql_lines = sum(1 for line in ZONE_SQL.strip().splitlines() if line.strip())
    print(f"zone {zone}, {limit} incidents")
    print(f"  hand-coded: {len(hand)} rows, 7 queries over 4 databases, {counted_lines()} lines of SQL and Python, "
          f"{hand_s:.2f} s")
    print(f"  Zetaris:    {len(fed)} rows, 1 federated query, {sql_lines} lines of SQL, {fed_s:.2f} s")
    print(f"  same rows: {same}")
    if not same:
        for a, b in zip(hand, fed):
            diff = [(i, x, y) for i, (x, y) in enumerate(zip(a, b)) if norm(x) != norm(y)]
            if diff:
                print("  first difference:", a[0], diff[:3])
                break


if __name__ == "__main__":
    os.chdir(ROOT)
    main(*(sys.argv[1:2] or ["FINAL"]))
