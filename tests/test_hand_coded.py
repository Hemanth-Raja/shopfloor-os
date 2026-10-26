"""compare/hand-coded/zone_read.py: the hand-coded zone read joins like ZONE_SQL's LEFT JOINs. No network, no Docker."""

import importlib.util
from pathlib import Path

from app.engines.shopfloor_world import ZONE_COLUMNS

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("zone_read", ROOT / "compare" / "hand-coded" / "zone_read.py")
zr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(zr)

INC = ["INC-1", "2026-10-16 10:00:00", "A", "agv_fault", "nav", "FINAL", "F30", "", "AGV-3", "high", "300", "2.5", "2",
       "900", "cleared", "f", "", "W-19", "rec-1"]
TABLES = {"stations": [["F30", "Seats and doors"]], "machines": [], "recordings": [["rec-1", "normal", "0.12"]],
          "workers": [["W-19", "Sanjay", "maintenance_tech", "machine_repair"]], "loto": [["W-19", "2027-01-01"]],
          "agvs": [["AGV-3", "KT300", "80", "100", "2026-05-21"]]}


def test_fetch_asks_each_database_only_for_the_rows_the_incidents_point_to():
    seen = []

    def run(db, sql):
        seen.append((db, sql))
        return [INC] if db == "ops" else []
    t = zr.fetch("FINAL", 8, run=run)
    assert [d for d, _ in seen] == ["ops", "plant", "plant", "plant", "hr", "hr", "fleet"] and t["incidents"] == [INC]
    assert "station_id IN ('F30')" in seen[1][1] and "machine_id IN (NULL)" in seen[2][1]  # an empty id matches nothing
    assert "cert = 'lockout_tagout' AND worker_id IN ('W-19')" in seen[5][1] and "LIMIT 8" in seen[0][1]


def test_join_fills_zone_sql_columns_and_leaves_missing_matches_empty():
    row = zr.join({"incidents": [INC], **TABLES})[0]
    got = dict(zip(ZONE_COLUMNS, row))
    assert len(row) == len(ZONE_COLUMNS) and got["station_name"] == "Seats and doors" and got["machine_name"] == ""
    assert got["worker_loto_expires"] == "2027-01-01" and got["agv_faults_90d"] == "100" and got["vibration_label"] == "normal"


def test_values_compare_across_psql_and_zetaris_formats():
    assert zr.norm("t") == zr.norm("true") and zr.norm("2.50") == zr.norm(2.5)
    assert zr.norm("2026-10-16 10:00:00") == zr.norm("2026-10-16 10:00:00.0") and zr.norm(None) == ""
    assert 20 < zr.counted_lines() < 60
