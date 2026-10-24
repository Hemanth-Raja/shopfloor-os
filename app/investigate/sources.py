"""Where the investigator's SQL runs.

- Live: Zetaris (app/runtime.run_sql, behind its circuit breaker).
- SAMPLE_MODE=record: run on Zetaris and save the rows to logs/fixtures/sql-<hash>.json.
- SAMPLE_MODE=true: return the recorded rows; for SQL that was never recorded, run it on an offline SQLite copy
  of the four sources, built once from the data generator into data/cache/offline.sqlite. That is best effort:
  Spark-only functions may fail, and the error is reported as evidence rather than crashing the investigation.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "logs" / "fixtures"
OFFLINE_DB = ROOT / "data" / "cache" / "offline.sqlite"
DBS = ("fleet", "hr", "plant", "ops")


def _key(sql: str) -> str:
    return "sql-" + hashlib.sha256(" ".join(sql.split()).encode()).hexdigest()[:20]


def _build_offline() -> None:
    from app.data import generate as g
    g.rng.seed(42)
    sql, _ = g.build()
    OFFLINE_DB.parent.mkdir(parents=True, exist_ok=True)
    tmp = OFFLINE_DB.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    for db in DBS:
        con.execute(f"ATTACH DATABASE '{tmp.parent / f'offline_{db}.sqlite'}' AS sf_{db}")
    for db, parts in sql.items():
        for part in parts:
            part = re.sub(r"\bCREATE TABLE (\w+)", rf"CREATE TABLE IF NOT EXISTS sf_{db}.\1", part)
            part = re.sub(r"\bINSERT INTO (\w+)", rf"INSERT INTO sf_{db}.\1", part)
            con.executescript(part)
    con.commit()
    con.close()
    tmp.replace(OFFLINE_DB)


def _spark_functions(con: sqlite3.Connection) -> None:
    def dt(v):
        return datetime.fromisoformat(str(v).replace(" ", "T")) if v else None
    con.create_function("hour", 1, lambda v: dt(v).hour if v else None)
    con.create_function("to_date", 1, lambda v: str(v)[:10] if v else None)
    con.create_function("dayofweek", 1, lambda v: (dt(v).isoweekday() % 7) + 1 if v else None)
    con.create_function("month", 1, lambda v: dt(v).month if v else None)
    con.create_function("year", 1, lambda v: dt(v).year if v else None)
    con.create_function("concat", -1, lambda *a: "".join("" if x is None else str(x) for x in a))
    con.create_function("date_trunc", 2, lambda unit, v: (str(v)[:10] if str(unit).lower() == "day" else
                                                          str(v)[:13] + ":00:00" if str(unit).lower() == "hour" else str(v)))


def offline_run_sql(sql: str) -> list[list]:
    if not OFFLINE_DB.exists():
        _build_offline()
    con = sqlite3.connect(OFFLINE_DB)
    try:
        for db in DBS:
            con.execute(f"ATTACH DATABASE '{OFFLINE_DB.parent / f'offline_{db}.sqlite'}' AS sf_{db}")
        _spark_functions(con)
        return [list(r) for r in con.execute(sql).fetchall()]
    finally:
        con.close()


def sql_runner(mode: str, live: Callable | None = None) -> Callable[[str], list[list]]:
    """A run_sql for the investigator according to SAMPLE_MODE ('false' | 'record' | 'true')."""
    if mode == "true":
        def replay(sql: str) -> list[list]:
            path = FIXTURES / f"{_key(sql)}.json"
            if path.exists():
                return json.loads(path.read_text())["rows"]
            return offline_run_sql(sql)
        return replay
    if live is None:
        from app.runtime import run_sql as live
    if mode == "record":
        def record(sql: str) -> list[list]:
            rows = live(sql)
            FIXTURES.mkdir(parents=True, exist_ok=True)
            (FIXTURES / f"{_key(sql)}.json").write_text(json.dumps({"sql": sql, "rows": rows}, default=str))
            return rows
        return record
    return live
