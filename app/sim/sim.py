"""Deterministic tick-based floor simulation with an incident injector. No LLM calls (AGENTS.md rule 5).

One tick is 2 s and agents move one 2 m cell per tick (1 m/s). An AGV whose route sets a crossing speed of
0.5 m/s or less spends two ticks on every pedestrian-crossing cell. Moves are planned with space-time A*
against a shared reservation table, and the step loop also refuses to move an agent into an occupied cell,
so even a stalled AGV cannot cause a collision.
"""

from __future__ import annotations

import copy
import csv
import random
from dataclasses import dataclass, field
from pathlib import Path

from app.contracts import Incident
from app.sim.grid import Grid
from app.sim.pathing import Cell, Reservations, plan_path

TICK_S = 2.0
SEED_DIR = Path(__file__).resolve().parents[2] / "data" / "seed"


@dataclass
class Agent:
    id: str
    kind: str  # worker | agv
    pos: Cell
    path: list[Cell] = field(default_factory=list)
    path_start: int = 0
    status: str = "idle"  # idle | moving | stopped | fault | stalled
    goal: Cell | None = None
    speed_mps: float = 1.0


def slow_at_crossings(grid: Grid, path: list[Cell], crossing_speed_mps: float | None) -> list[Cell]:
    """Repeat each crossing cell so the AGV spends 2 ticks there when it slows to 0.5 m/s or less."""
    if crossing_speed_mps is None or crossing_speed_mps > 0.5:
        return path
    out: list[Cell] = []
    for i, cell in enumerate(path):
        out.append(cell)
        if i and grid.is_crossing(*cell) and path[i - 1] != cell:
            out.append(cell)
    return out


class Sim:
    def __init__(self, grid: Grid, seed: int = 42):
        self.grid = grid
        self.seed = seed
        self.rng = random.Random(seed)
        self.tick = 0
        self.agents: dict[str, Agent] = {}
        self.res = Reservations()
        self.geofence: set[Cell] = set()
        self.stalls: dict[str, int] = {}  # agent -> tick the stall starts
        self.collisions: list[tuple[int, Cell, str, str]] = []
        self.history: list[dict[str, Cell]] = []
        self._incident_seq = 0

    # ---------------------------------------------------------------- setup

    @classmethod
    def from_plant(cls, plant, grid: Grid, seed: int = 42, shift: str | None = None) -> "Sim":
        """Place workers (optionally only one shift) and AGVs at their world-model positions."""
        sim = cls(grid, seed)
        taken: set[Cell] = set()
        for type_ in ("agv", "worker"):
            for e in plant.entities(type_):
                if type_ == "worker" and shift and e["attrs"].get("shift") != shift:
                    continue
                cell = sim._free_cell_near(e["attrs"]["x"], e["attrs"]["y"], taken)
                taken.add(cell)
                sim.add_agent(e["id"], type_, cell)
        return sim

    def _free_cell_near(self, x: int, y: int, taken: set[Cell]) -> Cell:
        cands = sorted((c for c in self.grid.cells if self.grid.walkable(*c) and c not in taken),
                       key=lambda c: (abs(c[0] - x) + abs(c[1] - y), c))
        return cands[0]

    def add_agent(self, agent_id: str, kind: str, cell: Cell) -> Agent:
        agent = Agent(agent_id, kind, cell)
        self.agents[agent_id] = agent
        self.res.park(agent_id, cell)
        return agent

    def clone(self) -> "Sim":
        return copy.deepcopy(self)

    # ---------------------------------------------------------------- commands

    def assign(self, agent_id: str, goal: Cell, speed_mps: float = 1.0, crossing_speed_mps: float | None = None,
               via: Cell | None = None) -> list[Cell] | None:
        """Plan a collision-free path (optionally through a pickup cell `via`) to the nearest free walkable
        cell at `goal`, and start moving."""
        agent = self.agents[agent_id]
        others = {a.pos for a in self.agents.values() if a.id != agent_id} | self.geofence
        target = self._free_cell_near(*goal, others)
        self.res.release(agent_id)
        blocked = self.geofence - {agent.pos}
        path = None
        if via is not None:
            pickup = self._free_cell_near(*via, others)
            leg1 = plan_path(self.grid, agent.pos, pickup, self.tick, self.res, agent_id, blocked)
            if leg1 is not None:
                self.res.reserve_path(agent_id, leg1, self.tick, park=False)
                leg2 = plan_path(self.grid, pickup, target, self.tick + len(leg1) - 1, self.res, agent_id, blocked)
                self.res.release(agent_id)
                path = leg1 + leg2[1:] if leg2 is not None else None
        else:
            path = plan_path(self.grid, agent.pos, target, self.tick, self.res, agent_id, blocked)
        if path is None:
            self.res.park(agent_id, agent.pos)
            return None
        if agent.kind == "agv":
            path = slow_at_crossings(self.grid, path, crossing_speed_mps)
        self.res.reserve_path(agent_id, path, self.tick)
        agent.path, agent.path_start, agent.goal = path, self.tick, target
        agent.status, agent.speed_mps = "moving", speed_mps
        return path

    def stop(self, agent_id: str, status: str = "stopped") -> None:
        agent = self.agents[agent_id]
        agent.path, agent.goal, agent.status = [], None, status
        self.res.park(agent_id, agent.pos)

    def set_geofence(self, cells: set[Cell]) -> None:
        self.geofence = set(cells)

    def inject_stall(self, agent_id: str, at_tick: int) -> None:
        self.stalls[agent_id] = at_tick

    # ---------------------------------------------------------------- loop

    def step(self) -> None:
        self.tick += 1
        occupied = {a.pos: a.id for a in self.agents.values()}
        for agent in sorted(self.agents.values(), key=lambda a: a.id):
            if agent.status != "moving" or not agent.path:
                continue
            if agent.id in self.stalls and self.tick >= self.stalls[agent.id]:
                agent.status = "stalled"
                self.res.park(agent.id, agent.pos)
                continue
            i = min(self.tick - agent.path_start, len(agent.path) - 1)
            nxt = agent.path[i]
            if nxt != agent.pos and nxt in occupied and occupied[nxt] != agent.id:
                agent.path_start += 1  # someone is there (e.g. a stalled AGV): wait one tick
                continue
            occupied.pop(agent.pos, None)
            agent.pos = nxt
            occupied[nxt] = agent.id
            if i == len(agent.path) - 1 and agent.pos == agent.path[-1]:
                agent.status = "idle"
        seen: dict[Cell, str] = {}
        for agent in self.agents.values():
            if agent.pos in seen:
                self.collisions.append((self.tick, agent.pos, seen[agent.pos], agent.id))
            seen[agent.pos] = agent.id
        self.history.append({a.id: a.pos for a in self.agents.values()})

    def run(self, ticks: int) -> None:
        """Headless fast mode: no rendering, no sleeping."""
        for _ in range(ticks):
            self.step()

    # ---------------------------------------------------------------- incidents

    def inject(self, kind: str, plant, station_id: str | None = None, agv_id: str | None = None,
               false_alarm: bool | None = None, severity: str | None = None) -> Incident:
        """Create an incident in the sim. Sensor readings are seeded, so the same seed gives the same incident."""
        self._incident_seq += 1
        inc_id = f"SIM-{self.seed}-{self._incident_seq:03d}"
        sensors: dict = {}
        machine_id = None
        if kind == "agv_fault":
            agv = self.agents[agv_id]
            x, y = plant.station_xy(station_id) if station_id else agv.pos
            # stuck on the south lane a few cells upstream of the station it was feeding
            cell = self._free_cell_near(x - 4, 13, {a.pos for a in self.agents.values() if a.id != agv_id})
            self.res.release(agv_id)
            agv.pos = cell
            self.stop(agv_id, status="fault")
            sensors = {"fault_code": self.rng.choice(["E12_WHEEL_SLIP", "E40_BATTERY_LOW", "E52_NAV_LOST"])}
            x, y = cell
        else:
            x, y = plant.station_xy(station_id)
            if kind == "overheat":
                machine = next((m for m in plant.entities("machine") if m["attrs"]["station_id"] == station_id), None)
                machine_id = machine["id"] if machine else None
                false_alarm = bool(false_alarm)
                sensors = {"process_temp_k": round(self.rng.uniform(313.5, 316.0), 1),
                           "temp_threshold_k": 313.0, "air_temp_k": round(self.rng.uniform(300, 303), 1),
                           **self._vibration("normal" if false_alarm else "abnormal")}
            elif kind == "spill":
                sensors = {"camera_confidence": round(self.rng.uniform(0.8, 0.97), 2), "fluid": "coolant"}
            elif kind == "part_shortage":
                sensors = {"line_side_stock_min": round(self.rng.uniform(2.0, 6.0), 1)}
        zone = self.grid.label(x, y) or "FLOOR"
        return Incident(id=inc_id, kind=kind, station_id=station_id, zone_id=zone, x=x, y=y, tick=self.tick,
                        severity=severity or {"part_shortage": "high", "agv_fault": "medium", "overheat": "high",
                                              "spill": "high"}[kind],
                        agv_id=agv_id, machine_id=machine_id,
                        sensors=sensors, truth_false_alarm=false_alarm if kind == "overheat" else False)

    def _vibration(self, label: str) -> dict:
        path = SEED_DIR / "bosch_cnc_vibration_summary.csv"
        if not path.exists():
            return {"vibration_label": label}
        with path.open(newline="") as f:
            rows = [r for r in csv.DictReader(f) if r["label"] == label]
        r = self.rng.choice(rows)
        return {"vibration_recording": r["recording"], "vibration_label": label,
                "vibration_x_rms": float(r["x_rms"]), "vibration_z_kurtosis": float(r["z_kurtosis"])}
