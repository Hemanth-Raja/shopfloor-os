"""Offline fixtures: a plant world built from generate.py constants (no Zetaris) and a fake planner LLM."""

from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import pytest

from app.contracts import Plan
from app.data import generate as g
from app.engines.plant_world import PlantWorld
from app.sim.grid import Grid
from app.sim.sim import Sim

TODAY = date(2026, 10, 16)
LAPSED = {("W-22", "lockout_tagout"): "2026-08-29", ("W-21", "forklift"): "2026-08-14"}


def plant_rows() -> list[dict]:
    grid = Grid.from_generator()
    zone_cell = {z[0]: grid.nearest_walkable((z[3] + z[5]) // 2, (z[4] + z[6]) // 2) for z in g.ZONES}
    rows = []
    for s in g.STATIONS:
        rows.append({"row_kind": "station", "id": s[0], "name": s[1], "x": s[3], "y": s[4], "a": s[2],
                     "b": str(s[5]).lower(), "c": s[6], "d": None})
    station = {s[0]: s for s in g.STATIONS}
    for m in g.MACHINES:
        st = station[m[3]]
        rows.append({"row_kind": "machine", "id": m[0], "name": m[1], "x": st[3], "y": st[4], "a": m[3], "b": m[2],
                     "c": st[2], "d": None})
    for i, (role, shift, zone, stations, skills) in enumerate(g.CREW):
        wid = f"W-{i + 1:02d}"
        x, y = (station[stations[0]][3], station[stations[0]][4]) if stations else zone_cell[zone]
        rows.append({"row_kind": "worker", "id": wid, "name": g.NAMES[i], "x": x, "y": y, "a": role, "b": shift,
                     "c": ",".join(skills), "d": ",".join(stations) or None})
        for cert in skills:
            if cert in g.CERTS:
                rows.append({"row_kind": "cert", "id": wid, "name": cert, "x": None, "y": None,
                             "a": LAPSED.get((wid, cert), "2027-06-30"), "b": None, "c": None, "d": None})
    for i, a in enumerate(g.AGVS):
        flaky = a[0] == g.FLAKY_AGV
        rows.append({"row_kind": "agv", "id": a[0], "name": a[1], "x": 27 + i, "y": 17 + i % 2, "a": a[2],
                     "b": 62 if flaky else 90, "c": 100 if flaky else 20, "d": "2026-05-21" if flaky else "2026-10-10"})
    for r in g.SAFETY_RULES:
        rows.append({"row_kind": "rule", "id": r[0], "name": r[1], "x": None, "y": None, "a": r[3], "b": r[4],
                     "c": r[2], "d": None})
    for kind, cost in (("part_shortage", 5350), ("agv_fault", 900), ("machine_overheat", 9000), ("fluid_spill", 1200)):
        rows.append({"row_kind": "stat", "id": kind, "name": None, "x": None, "y": None, "a": cost, "b": 3, "c": 4, "d": 100})
    return rows


@pytest.fixture
def plant():
    return PlantWorld(plant_rows(), TODAY, "2026-10-16T14:00:00.000Z")


@pytest.fixture
def grid():
    return Grid.from_generator()


@pytest.fixture
def sim(plant, grid):
    return Sim.from_plant(plant, grid, seed=7, shift="B")


class FakePlanner:
    """Stands in for chat_json: picks the top candidates; omits the crossing speed until the critic says so."""

    def __init__(self, unavailable: bool = False):
        self.calls = 0
        self.unavailable = unavailable

    def __call__(self, lane, messages, schema, **kwargs):
        from app.llm import LLMUnavailable
        self.calls += 1
        if self.unavailable:
            raise LLMUnavailable("fake timeout")
        payload = json.loads(messages[1]["content"])
        inc = payload["incident"]
        fixed = any("crossing_speed_mps" in m["content"] for m in messages[2:])
        workers = [c["id"] for c in payload["worker_candidates"][:1]]
        agvs = payload["agv_candidates"][:1]
        routes = [{"agv_id": a["id"], "to_station": inc["station_id"], "speed_mps": 1.0,
                   "crossing_speed_mps": 0.5 if fixed else None} for a in agvs if inc.get("station_id")]
        plan = Plan(incident_id="x", assignee_ids=workers or [a["id"] for a in agvs], agv_routes=routes,
                    priority="high", rationale="fake planner")
        return SimpleNamespace(data=plan, text="", model="fake", latency_s=0.0)
