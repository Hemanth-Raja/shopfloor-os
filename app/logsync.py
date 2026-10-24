"""Agent logs into the data layer: trace steps and model calls copied to Postgres and queried through Zetaris.

The JSONL files in logs/ stay the source of truth (they work offline and survive a Zetaris outage). This copies
new lines into two tables in the ops database, which Zetaris federates as sf_ops.agent_steps and sf_ops.llm_calls,
next to sf_ops.journal (the hash-chained shift journal, replicated by app/journal.py). Ask the floor can then answer
questions about the agents' own history with federated SQL.

Idempotent: each row's key is a hash of its source line, and a small offsets file remembers how far each log file
was read. Writes go to the local Postgres container (docker exec psql), like the journal replica.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

LOGS = Path(__file__).resolve().parents[1] / "logs"
OFFSETS = LOGS / ".logsync.json"
BATCH = 200
DDL = ("CREATE TABLE IF NOT EXISTS agent_steps (id text PRIMARY KEY, ts timestamptz, run_id text, incident text, "
       "agent text, ok boolean, ms double precision, attempt int, model text, decision text, rules text, detail jsonb);\n"
       "CREATE TABLE IF NOT EXISTS llm_calls (id text PRIMARY KEY, ts timestamptz, role text, requested_model text, "
       "answering_model text, latency_s double precision, prompt_tokens int, completion_tokens int, truncated boolean);")


def _lit(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


def step_row(line: str) -> str | None:
    try:
        s = json.loads(line)
    except json.JSONDecodeError:
        return None
    if "agent" not in s:
        return None
    d = s.get("detail") or {}
    call = d.get("model_call") or (d.get("route") or {}).get("model_call") or {}
    rules = ",".join(sorted({v.get("rule_id", "") for v in d.get("violations", []) if isinstance(v, dict)})) or None
    detail = json.dumps(d, default=str)
    if len(detail) > 2000:  # keep the table light; the file has everything
        detail = json.dumps({k: d[k] for k in ("decision", "gate", "attempt", "assignees", "summary") if k in d}, default=str)
    key = hashlib.sha256(line.encode()).hexdigest()[:24]
    return "(" + ",".join([_lit(key), _lit(s.get("ts")), _lit(s.get("run_id")), _lit(s.get("incident")),
                           _lit(s.get("agent")), _lit(bool(s.get("ok"))), _lit(s.get("ms")), _lit(d.get("attempt")),
                           _lit(call.get("answering") or d.get("model")), _lit(d.get("decision")), _lit(rules),
                           _lit(detail) + "::jsonb"]) + ")"


def call_row(line: str) -> str | None:
    try:
        c = json.loads(line)
    except json.JSONDecodeError:
        return None
    t = c.get("tokens") or {}
    key = hashlib.sha256(line.encode()).hexdigest()[:24]
    return "(" + ",".join([_lit(key), _lit(c.get("ts")), _lit(c.get("lane")), _lit(c.get("requested_model")),
                           _lit(c.get("answering_model")), _lit(c.get("latency_s")), _lit(t.get("prompt")),
                           _lit(t.get("completion")), _lit(bool(c.get("truncated")))]) + ")"


def new_lines(path: Path, offsets: dict) -> tuple[list[str], int]:
    start = offsets.get(path.name, 0)
    size = path.stat().st_size
    if size < start:  # the file was rotated or truncated: read it again from the start
        start = 0
    with path.open("rb") as fh:
        fh.seek(start)
        data = fh.read()
    end = data.rfind(b"\n") + 1  # never read a half-written last line
    return [x for x in data[:end].decode("utf-8", "replace").splitlines() if x.strip()], start + end


def psql(sql: str, container: str = "shopfloor-pg") -> None:
    out = subprocess.run(["docker", "exec", "-i", container, "psql", "-U", "postgres", "-d", "ops", "-v",
                          "ON_ERROR_STOP=1", "-q"], input=sql, capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip()[:200])


def statements(logs: Path = LOGS, offsets: dict | None = None) -> tuple[list[str], dict, dict]:
    """-> (SQL statements, new offsets, row counts). Pure: no database, so it is testable."""
    offsets = dict(offsets or {})
    sql, counts = [DDL], {"agent_steps": 0, "llm_calls": 0}
    files = [(p, "agent_steps", step_row) for p in sorted(logs.glob("trace*.jsonl"))]
    if (logs / "llm.jsonl").exists():
        files.append((logs / "llm.jsonl", "llm_calls", call_row))
    cols = {"agent_steps": "(id, ts, run_id, incident, agent, ok, ms, attempt, model, decision, rules, detail)",
            "llm_calls": "(id, ts, role, requested_model, answering_model, latency_s, prompt_tokens, completion_tokens, "
                         "truncated)"}
    for path, table, fn in files:
        lines, end = new_lines(path, offsets)
        rows = [r for r in map(fn, lines) if r]
        for i in range(0, len(rows), BATCH):
            sql.append(f"INSERT INTO {table} {cols[table]} VALUES {','.join(rows[i:i + BATCH])} ON CONFLICT (id) DO NOTHING;")
        counts[table] += len(rows)
        offsets[path.name] = end
    return sql, offsets, counts


def sync(logs: Path = LOGS, run=psql) -> dict:
    offsets = json.loads(OFFSETS.read_text()) if OFFSETS.exists() and logs == LOGS else {}
    sql, new, counts = statements(logs, offsets)
    for i in range(0, len(sql), 5):
        run("\n".join(sql[i:i + 5]))
    if logs == LOGS:
        OFFSETS.write_text(json.dumps(new))
    return counts


def sync_loop(every_s: float = 30.0) -> None:
    import time
    while True:
        try:
            sync()
        except Exception:  # noqa: BLE001 - the files are the source of truth; try again later
            pass
        time.sleep(every_s)
