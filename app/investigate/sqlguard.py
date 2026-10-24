"""SQL guard: only one read-only SELECT over whitelisted sf_* tables, with a row limit. No model calls.

Anything else is rejected with a reason that goes back to the SQL agent for its single retry.
"""

from __future__ import annotations

import re

from app.investigate.catalog import AGENT_CATALOG, CATALOG

MAX_ROWS = 200
FORBIDDEN = ("insert", "update", "delete", "drop", "create", "alter", "merge", "grant", "revoke", "truncate",
             "register", "refresh", "uncache", "call", "set", "use", "load", "cache", "replace", "describe", "show",
             "msck", "analyze", "export", "copy")


class GuardError(ValueError):
    """The SQL was refused; the message says why."""


def _strip_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    return re.sub(r"--[^\n]*", " ", sql)


def _strip_strings(sql: str) -> str:
    return re.sub(r"'(?:[^']|'')*'", "''", sql)


def check(sql: str) -> str:
    """Return the cleaned, limited SQL, or raise GuardError."""
    text = _strip_comments(sql or "").strip().rstrip(";").strip()
    if not text:
        raise GuardError("empty SQL")
    bare = _strip_strings(text)
    if ";" in bare:
        raise GuardError("only one statement is allowed (no ';')")
    low = bare.lower()
    if not re.match(r"^(select|with)\b", low):
        raise GuardError("only SELECT queries are allowed")
    bad = [w for w in FORBIDDEN if re.search(rf"\b{w}\b", low)]
    if bad:
        raise GuardError(f"forbidden keyword(s): {', '.join(bad)}")
    refs = set(re.findall(r"\b(sf_[a-z]+)\.([a-z_]+)\b", low))
    if not refs:
        raise GuardError("query must read at least one qualified table such as sf_ops.incidents")
    unknown = sorted(f"{s}.{t}" for s, t in refs if f"{s}.{t}" not in CATALOG and f"{s}.{t}" not in AGENT_CATALOG)
    if unknown:
        raise GuardError(f"unknown table(s): {', '.join(unknown)}; allowed: {', '.join(sorted(CATALOG | AGENT_CATALOG))}")
    for m in re.finditer(r"\b(?:from|join)\s+([a-z_][a-z0-9_]*)(?!\.)\b(?!\s*\()", low):
        if m.group(1) not in ("select", "lateral") and not m.group(1).startswith("sf_"):
            # a bare table name or a CTE: allow CTE names declared with WITH ... AS
            if not re.search(rf"\bwith\b.*\b{m.group(1)}\s+as\s*\(", low, re.S) and \
               not re.search(rf",\s*{m.group(1)}\s+as\s*\(", low, re.S):
                raise GuardError(f"table '{m.group(1)}' must be qualified as sf_<db>.<table>")
    limit = re.search(r"\blimit\s+(\d+)\s*$", low)
    if limit:
        if int(limit.group(1)) > MAX_ROWS:
            text = re.sub(r"(?i)\blimit\s+\d+\s*$", f"LIMIT {MAX_ROWS}", text)
    else:
        text = f"{text}\nLIMIT {MAX_ROWS}"
    return text
