"""Thin client for the Zetaris SQL REST API, plus datasource registration.

    python -m app.zetaris register         # create sf_fleet, sf_hr, sf_plant, sf_ops (skips existing)
    python -m app.zetaris refresh          # pick up new tables and columns after scripts/load_data.sh
    python -m app.zetaris "SELECT 1"       # run one statement, print rows
"""

import json
import os
import sys
import uuid

import requests
from dotenv import load_dotenv

load_dotenv()

DATABASES = ["fleet", "hr", "plant", "ops"]


def _scrub(text):
    """Never let the Postgres password reach logs or errors."""
    secret = os.environ.get("PG_PASSWORD")
    return text.replace(secret, "***") if secret else text


def run_sql(sql, limit=1000):
    """Run one statement through Zetaris and return the rows (list of lists)."""
    response = requests.post(
        os.environ["ZETARIS_REST_URL"].rstrip("/") + "/sql-editor/sqls/run",
        headers={
            "Authorization": "Bearer " + os.environ["ZETARIS_API_KEY"],
            "X-Org-ID": os.environ["ZETARIS_ORG_ID"],
            "X-Request-ID": str(uuid.uuid4()),
        },
        json={"queryId": str(uuid.uuid4()), "sql": sql, "source": "SqlEditor", "limit": limit},
        timeout=300,
    )
    if not response.ok:
        raise RuntimeError(_scrub(f"Zetaris HTTP {response.status_code}: {response.text[:500]}"))
    body = response.json()
    if body.get("error") or body.get("errorMessage"):
        raise RuntimeError(_scrub(f"Zetaris error: {json.dumps(body)[:500]}"))
    return body.get("data") or []


def datasources():
    return {str(row[0]).lower() for row in run_sql("SHOW DATASOURCES")}


def register(db):
    """Create the sf_<db> datasource pointing at shopfloor-pg and import all its tables."""
    name = f"sf_{db}"
    if name not in datasources():
        run_sql(
            f'CREATE DATASOURCE {name} DESCRIBE BY "PostgreSQL" OPTIONS ('
            f'jdbcdriver "org.postgresql.Driver", '
            f'jdbcurl "jdbc:postgresql://shopfloor-pg:5432/{db}", '
            f'username "postgres", password "{os.environ["PG_PASSWORD"]}", fetchsize "1000")'
        )
        print(f"created {name}")
    else:
        print(f"{name} already exists")
    run_sql(f"REGISTER DATASOURCE TABLES FROM {name}")
    print(f"registered tables for {name}")


def refresh(db):
    """Register any new tables, re-import metadata and drop cached statistics after a reload."""
    name = f"sf_{db}"
    for sql in (f"REGISTER DATASOURCE TABLES FROM {name}", f"REFRESH DATASOURCE TABLES FROM {name}",
                f"UNCACHE DATASOURCE {name}"):
        run_sql(sql)
    print(f"refreshed {name}")


if __name__ == "__main__":
    if sys.argv[1:] in (["register"], ["refresh"]):
        for db in DATABASES:
            (register if sys.argv[1] == "register" else refresh)(db)
    else:
        for row in run_sql(" ".join(sys.argv[1:])):
            print(row)
