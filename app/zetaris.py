"""Thin client for the Zetaris SQL REST API, plus datasource registration.

    python -m app.zetaris register         # create sf_fleet, sf_hr, sf_plant, sf_ops (skips existing)
    python -m app.zetaris refresh          # pick up new tables and columns after scripts/load_data.sh
    python -m app.zetaris reader-role      # create the agents' read-only role and its SELECT grants
    python -m app.zetaris "SELECT 1"       # run one statement, print rows (operator login)

Two logins. Agent reads use ZETARIS_AGENT_API_KEY, a Zetaris user whose only role is sf_agent_reader (SELECT on the
sf_* datasources), so a write from an agent is refused by Zetaris itself, not only by our SQL guard. The operator
login (ZETARIS_API_KEY) is kept for register/refresh, the Lab write and this command line. Without an agent key the
agents fall back to the operator login and the SQL guard is the only read-only check (health reports which).
"""

import json
import os
import sys
import uuid

import requests
from dotenv import load_dotenv

load_dotenv()

DATABASES = ["fleet", "hr", "plant", "ops"]
READER_ROLE = "sf_agent_reader"


def reader() -> str:
    """Which login agent reads use: 'read-only role' or 'operator (no ZETARIS_AGENT_API_KEY)'."""
    return "read-only role" if os.environ.get("ZETARIS_AGENT_API_KEY") else "operator (no ZETARIS_AGENT_API_KEY)"


def _scrub(text):
    """Never let the Postgres password reach logs or errors."""
    for name in ("PG_PASSWORD", "ZETARIS_API_KEY", "ZETARIS_AGENT_API_KEY"):
        secret = os.environ.get(name)
        if secret:
            text = text.replace(secret, "***")
    return text


def run_sql(sql, limit=1000, write=False):
    """Run one statement through Zetaris and return the rows (list of lists). Reads use the agents' read-only login
    when it is set; write=True uses the operator login."""
    key = os.environ["ZETARIS_API_KEY"] if write else (os.environ.get("ZETARIS_AGENT_API_KEY")
                                                        or os.environ["ZETARIS_API_KEY"])
    response = requests.post(
        os.environ["ZETARIS_REST_URL"].rstrip("/") + "/sql-editor/sqls/run",
        headers={
            "Authorization": "Bearer " + key,
            "X-Org-ID": os.environ["ZETARIS_ORG_ID"],
            "X-Request-ID": str(uuid.uuid4()),
        },
        json={"queryId": str(uuid.uuid4()), "sql": sql, "source": "SqlEditor", "limit": limit},
        timeout=60,  # bounded: a Zetaris request should answer within a minute
    )
    if not response.ok:
        raise RuntimeError(_scrub(f"Zetaris HTTP {response.status_code}: {response.text[:500]}"))
    body = response.json()
    if body.get("error") or body.get("errorMessage"):
        raise RuntimeError(_scrub(f"Zetaris error: {json.dumps(body)[:500]}"))
    return body.get("data") or []


def datasources():
    return {str(row[0]).lower() for row in run_sql("SHOW DATASOURCES", write=True)}


def register(db):
    """Create the sf_<db> datasource pointing at shopfloor-pg and import all its tables."""
    name = f"sf_{db}"
    if name not in datasources():
        run_sql(
            f'CREATE DATASOURCE {name} DESCRIBE BY "PostgreSQL" OPTIONS ('
            f'jdbcdriver "org.postgresql.Driver", '
            f'jdbcurl "jdbc:postgresql://shopfloor-pg:5432/{db}", '
            f'username "postgres", password "{os.environ["PG_PASSWORD"]}", fetchsize "1000")', write=True
        )
        print(f"created {name}")
    else:
        print(f"{name} already exists")
    run_sql(f"REGISTER DATASOURCE TABLES FROM {name}", write=True)
    print(f"registered tables for {name}")


def refresh(db):
    """Register any new tables, re-import metadata and drop cached statistics after a reload."""
    name = f"sf_{db}"
    for sql in (f"REGISTER DATASOURCE TABLES FROM {name}", f"REFRESH DATASOURCE TABLES FROM {name}",
                f"UNCACHE DATASOURCE {name}"):
        run_sql(sql, write=True)
    print(f"refreshed {name}")


def reader_role_sql(user=None):
    """The statements that make the agents' login read-only: a role with SELECT on each sf_* datasource."""
    out = [] if READER_ROLE.upper() in {str(r[0]).upper() for r in run_sql("SHOW ROLES", write=True)} else \
        [f"CREATE ROLE '{READER_ROLE}' DESCRIBE BY 'Shop-Floor OS agents, SELECT only on sf_ datasources'"]
    for db in DATABASES:
        out += [f"ASSIGN DATASOURCE sf_{db} TO ROLE '{READER_ROLE}'",
                f"GRANT SELECT ON DATASOURCE NAMESPACE sf_{db} TO ROLE '{READER_ROLE}'"]
    if user:
        out.append(f"ASSIGN USER '{user}' TO ROLE '{READER_ROLE}'")
    return out


if __name__ == "__main__":
    if sys.argv[1:] in (["register"], ["refresh"]):
        for db in DATABASES:
            (register if sys.argv[1] == "register" else refresh)(db)
    elif sys.argv[1:] == ["reader-role"]:
        for sql in reader_role_sql(os.environ.get("ZETARIS_AGENT_USER")):
            run_sql(sql, write=True)
            print(sql)
    else:
        for row in run_sql(" ".join(sys.argv[1:]), write=True):
            print(row)
