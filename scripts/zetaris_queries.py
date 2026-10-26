"""Run the exploratory queries in zetaris/queries/*.sql through Zetaris and write zetaris/queries/RESULTS.md.

    .venv/bin/python scripts/zetaris_queries.py            # every file
    .venv/bin/python scripts/zetaris_queries.py sf_hr usl  # some files

Each query is a `-- Q: <question>` line followed by one read-only statement (no semicolon). Reads go through
app/zetaris.py run_sql, so they use the agents' read-only login when ZETARIS_AGENT_API_KEY is set.
"""

from __future__ import annotations

import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
QUERIES = ROOT / "zetaris" / "queries"
MAX_ROWS = 12


def parse(text: str) -> tuple[str, list[tuple[str, str]]]:
    """-> (file note, [(question, sql)]). The note is the comment block before the first question."""
    note, queries, question, sql = [], [], None, []
    for line in text.splitlines():
        m = re.match(r"--\s*Q:\s*(.+)", line)
        if m:
            if question:
                queries.append((question, "\n".join(sql).strip()))
            question, sql = m.group(1).strip(), []
        elif question:
            sql.append(line)
        elif line.startswith("--"):
            note.append(line.lstrip("- ").strip())
    if question:
        queries.append((question, "\n".join(sql).strip()))
    return " ".join(note), queries


def columns(sql: str) -> list[str] | None:
    """Output column names from the SELECT list (alias after AS, else the last identifier); None for SHOW."""
    m = re.match(r"\s*SELECT\s+(.*?)\s+FROM\s", sql, re.S | re.I)
    if not m:
        return None
    parts, depth, cur = [], 0, ""
    for ch in m.group(1):
        depth += (ch == "(") - (ch == ")")
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    names = []
    for p in parts:
        alias = re.search(r"\bAS\s+(\w+)\s*$", p.strip(), re.I)
        names.append(alias.group(1) if alias else re.split(r"[.\s]", p.strip())[-1])
    return names


def table(cols: list[str] | None, rows: list[list]) -> str:
    if not rows:
        return "_no rows_\n"
    cols = cols if cols and len(cols) == len(rows[0]) else [f"col{i + 1}" for i in range(len(rows[0]))]
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    out += ["| " + " | ".join("" if v is None else str(v) for v in r) + " |" for r in rows[:MAX_ROWS]]
    if len(rows) > MAX_ROWS:
        out.append(f"\n_{len(rows) - MAX_ROWS} more rows not shown._")
    return "\n".join(out) + "\n"


def main(names: list[str]) -> None:
    from app.zetaris import reader, run_sql
    files = [QUERIES / f"{n}.sql" for n in names] if names else sorted(QUERIES.glob("*.sql"))
    started = datetime.now().astimezone()
    doc = [f"# Zetaris query results\n\nRun on {started:%d %b %Y %H:%M %Z} by `python scripts/zetaris_queries.py` through "
           f"`app/zetaris.py` `run_sql` (login: {reader()}). Real rows from the local Zetaris instance; at most "
           f"{MAX_ROWS} rows shown per query.\n"]
    for f in files:
        note, queries = parse(f.read_text())
        doc.append(f"## {f.stem}\n\n{note}\n\nSource: [`{f.name}`]({f.name})\n")
        for question, sql in queries:
            t = time.perf_counter()
            try:
                rows, err = run_sql(sql), None
            except RuntimeError as e:  # keep going: a failed query is reported, not hidden
                rows, err = [], str(e)[:300]
            ms = round((time.perf_counter() - t) * 1000)
            print(f"{f.stem}: {question[:60]} -> {'ERROR' if err else len(rows)} ({ms} ms)", flush=True)
            doc.append(f"### {question}\n\n```sql\n{sql}\n```\n")
            doc.append(f"**Error:** `{err}`\n" if err else f"{len(rows)} rows, {ms} ms.\n\n{table(columns(sql), rows)}")
    (QUERIES / "RESULTS.md").write_text("\n".join(doc))
    print(f"wrote {QUERIES / 'RESULTS.md'}")


if __name__ == "__main__":
    main(sys.argv[1:])
