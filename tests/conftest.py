"""Offline fixtures: a plant world built from generate.py constants (no Zetaris) and a fake planner LLM."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.contracts import Plan
from app.sim.grid import Grid
from app.sim.offline import offline_plant, plant_rows  # noqa: F401 - plant_rows re-exported for tests
from app.sim.sim import Sim


@pytest.fixture
def plant():
    return offline_plant()


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
