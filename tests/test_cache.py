"""TTL caches: read-only Zetaris results, write invalidation, expiry, clause and history caches, Health stats."""

from app import cache


def test_reads_are_cached_and_a_write_clears_them():
    calls = []
    run = cache.cached_sql(lambda sql: calls.append(sql) or [[len(calls)]], name="t_sql")
    assert run("SELECT 1 FROM sf_ops.incidents") == [[1]]
    assert run("SELECT 1   FROM  sf_ops.incidents") == [[1]] and len(calls) == 1  # same statement, cached
    run("INSERT INTO sf_ops.experiments VALUES (1)")
    assert run("SELECT 1 FROM sf_ops.incidents") == [[3]]  # the write invalidated it
    s = cache.stats()["t_sql"]
    assert s["hits"] == 1 and s["invalidations"] == 1


def test_entries_expire():
    now = [0.0]
    c = cache.TTLCache("t_ttl", 10, clock=lambda: now[0])
    assert c.get("k", lambda: 1) == 1 and c.get("k", lambda: 2) == 1
    now[0] = 11
    assert c.get("k", lambda: 3) == 3


def test_clause_retrieval_and_incident_history_hit_the_cache(plant, sim):
    from app.dynamic import DynamicOrchestrator
    from app.engines.citations import default_index
    from tests.conftest import FakePlanner
    idx = default_index()
    idx.for_incident("spill", "spill at F20")
    idx.for_incident("spill", "spill at F20")
    assert cache.stats()["clauses"]["hits"] >= 1
    queries = []
    run_sql = lambda sql: queries.append(sql) or [[12, 9, 4.2, 900.0]]  # noqa: E731
    for _ in range(2):
        inc = sim.inject("overheat", plant, station_id="F10", false_alarm=True)
        DynamicOrchestrator(plant, sim, FakePlanner(), trace_path=None, run_sql=run_sql).handle(inc)
    assert len(queries) == 1 and cache.stats()["history"]["hits"] == 1  # the second overheat reused the history


def test_health_reports_cache_hit_rates(monkeypatch):
    from app import runtime
    monkeypatch.setattr(runtime, "check_health", lambda *a: {"overall": "ok"})
    out = runtime.health()
    assert "caches" in out and all("hit_rate" in v for v in out["caches"].values())


def test_bm25_ranks_the_matching_clause_first_and_rrf_fuses():
    from app.engines.bm25 import BM25, rrf
    b = BM25({"a": "slow down and sound the horn at cross aisles", "b": "keep walking surfaces dry and clean",
              "c": "lockout tagout before servicing machines"})
    assert b.rank("AGV too fast at the crossing aisle horn", ["a", "b", "c"])[0] == "a"
    assert rrf(["x", "y", "z"], ["y", "x", "z"])[:2] in (["x", "y"], ["y", "x"]) and rrf(["x"], ["x"]) == ["x"]
