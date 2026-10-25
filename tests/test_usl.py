"""The Unified Semantic Layer: one model over the four sources, deployed and activated in order. No network."""

from app import usl
from app.engines import shopfloor_world as sw


def test_compile_has_every_table_on_its_own_line_with_keys():
    sql = usl.compile_sql(deploy=True)
    assert sql.startswith(f"COMPILE USL floor DEPLOY NAMESPACE {usl.NAMESPACE} DDL\nCREATE TABLE ")
    assert sql.count("\nCREATE TABLE ") == len(usl.TABLES) and not sql.rstrip().endswith(";")
    assert sql.count("PRIMARY KEY") == len(usl.TABLES)
    assert "station_id varchar(20) FOREIGN KEY REFERENCES station(station_id)" in sql
    for name, (body, select) in usl.TABLES.items():
        cols = [line.strip().split()[0] for line in body.split(",\n")]
        assert select.startswith("SELECT COALESCE(") and all(c in select for c in cols)  # activation matches the DDL


def test_deploy_creates_compiles_activates_then_grants():
    calls = []
    usl.deploy(run=lambda sql, write=False: calls.append((sql.split("\n")[0][:40], write)), log=lambda *_: None)
    assert calls[0][0].startswith("CREATE NAMESPACE IF NOT EXISTS") and calls[1][0].startswith("COMPILE USL floor DEPLOY")
    assert [c[0].split(" AS ")[0] for c in calls[2:-1]] == [f"ACTIVATE USL TABLE {usl.PREFIX}.{t}"[:40] for t in usl.TABLES]
    assert calls[-1][0].startswith("GRANT SELECT ON USL NAMESPACE") and all(w for _, w in calls)  # admin login


def test_zone_query_reads_the_semantic_layer_and_falls_back_to_the_sources():
    sql = sw.zone_query("FINAL", 5, semantic=True)
    assert f"FROM {usl.PREFIX}.incident i" in sql and f"JOIN {usl.PREFIX}.certification c" in sql and "sf_" not in sql
    seen = []

    def run(q):
        seen.append(q)
        if usl.PREFIX in q:
            raise RuntimeError("table(...) is not activated")
        return []
    sw.zone_world("FINAL", run)
    assert len(seen) == 2 and sw.LAST_SOURCE == "sources"
