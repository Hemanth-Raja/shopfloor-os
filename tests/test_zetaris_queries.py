"""scripts/zetaris_queries.py: the committed query files parse into one statement per question. No network."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("zq", ROOT / "scripts" / "zetaris_queries.py")
zq = importlib.util.module_from_spec(spec)
spec.loader.exec_module(zq)


def test_every_query_file_has_read_only_statements_with_questions():
    files = sorted((ROOT / "zetaris" / "queries").glob("*.sql"))
    assert {f.stem for f in files} >= {"sf_fleet", "sf_hr", "sf_plant", "sf_ops", "usl", "dq"}
    for f in files:
        note, queries = zq.parse(f.read_text())
        assert note and 2 <= len(queries) <= 4
        for question, sql in queries:
            assert "?" in question and sql.split()[0].upper() in ("SELECT", "SHOW", "LIST") and ";" not in sql


def test_column_names_come_from_aliases_and_nested_calls_stay_whole():
    sql = "SELECT a.kind, round(avg(x), 1) AS avg_x, count(*) AS n FROM t a GROUP BY a.kind"
    assert zq.columns(sql) == ["kind", "avg_x", "n"] and zq.columns("SHOW TABLES IN sf_hr") is None
    assert zq.table(["a"], [[1], [None]]).splitlines()[-1] == "|  |"
