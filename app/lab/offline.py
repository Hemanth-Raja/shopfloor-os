"""Offline lab data: answers the lab's two history queries from the generator, with no Zetaris.

Used by the web UI's Lab panel in offline or replay mode. The incident history is the same generator output
that load_data.sh loads into Postgres, so the numbers match a live run on freshly loaded data.
"""

from __future__ import annotations

from collections import defaultdict
from functools import lru_cache

HISTORY_COLS = ["incident_id", "kind", "cause", "station_id", "zone_id", "x", "y", "time_to_clear_s",
                "spill_kit_distance", "supermarket_distance", "shift_code", "false_alarm"]


@lru_cache(maxsize=1)
def _incidents() -> tuple:
    from app.data import generate as g
    g.rng.seed(42)  # the generator's module-level RNG: reseed so the history is the canonical one
    plant = g.Plant()
    plant.run()
    return tuple(plant.incidents)


def offline_run_sql(sql: str) -> list[list]:
    incs = [i for i in _incidents() if not i["false_alarm"]]
    if "GROUP BY" in sql:  # SUMMARY_SQL
        groups: dict = defaultdict(list)
        for i in incs:
            if i["kind"] in ("fluid_spill", "part_shortage"):
                groups[(i["kind"], i["station_id"], i["zone_id"])].append(i)
        rows = []
        for (kind, station, zone), items in groups.items():
            avg = lambda k: round(sum(x[k] or 0 for x in items) / len(items), 1)  # noqa: E731
            rows.append([kind, station, zone, len(items), round(avg("time_to_clear_s")), avg("downtime_min"),
                         avg("spill_kit_distance"), avg("supermarket_distance")])
        return sorted(rows, key=lambda r: -r[3])[:12]
    kind = "fluid_spill" if "'fluid_spill'" in sql else "part_shortage"
    return [[i[c] for c in HISTORY_COLS] for i in incs if i["kind"] == kind]
