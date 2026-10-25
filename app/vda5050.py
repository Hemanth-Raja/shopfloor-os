"""VDA 5050 v2.0: the AGV interface a real fleet speaks, on top of the simulated floor.

Every AGV dispatch becomes a VDA 5050 `order` (nodes and edges in metres on the hall map, maxSpeed per edge with the
crossing speed where the route crosses a pedestrian lane, a `pick` action at the supermarket and a `drop` at line
side), and every AGV can report a VDA 5050 `state` (position, driving, battery, errors). The simulator still moves
the vehicles, so a real plant replaces only the transport: publish `order` on `uagv/v2/<manufacturer>/<serial>/order`
over MQTT and read `state` back, as the standard defines. No broker is bundled (no new dependency): messages go to
an in-memory ring for the UI and to logs/vda5050.jsonl. People are not AGVs and get no VDA 5050 messages.
"""

from __future__ import annotations

import itertools
import json
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from app.sim.grid import CELL_M

VERSION = "2.0.0"
MANUFACTURER = "ShopFloorOS-Sim"
MAP_ID = "final-assembly-hall"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _serial(agent_id: str) -> str:
    return agent_id.split(":")[-1]


def corners(path: list[tuple[int, int]], grid=None, keep=()) -> list[tuple[tuple[int, int], bool]]:
    """(cell, on a crossing) for the cells where the route turns or enters/leaves a crossing, plus both ends.
    A repeated cell (the sim's slow-down at a crossing) is one node."""
    cells = [c for i, c in enumerate(path) if i == 0 or c != path[i - 1]]
    crossing = (lambda c: bool(grid and grid.is_crossing(*c)))
    out = []
    for i, c in enumerate(cells):
        if i in (0, len(cells) - 1):
            out.append(c)
            continue
        a, b = cells[i - 1], cells[i + 1]
        turn = (c[0] - a[0], c[1] - a[1]) != (b[0] - c[0], b[1] - c[1])
        if turn or c in keep or crossing(c) != crossing(a) or crossing(c) != crossing(b):
            out.append(c)
    return [(c, crossing(c)) for c in out]


def order(agv_id: str, path: list[tuple[int, int]], speed_mps: float, crossing_speed_mps: float | None, *,
          order_id: str, update_id: int, header_id: int, grid=None, pickup: tuple[int, int] | None = None) -> dict:
    nodes, edges = [], []
    if pickup is not None and path:  # the pick happens where the route passes the supermarket exit
        pickup = min(path, key=lambda c: abs(c[0] - pickup[0]) + abs(c[1] - pickup[1]))
    pts = corners(path, grid, keep={tuple(pickup)} if pickup is not None else ())
    for i, ((x, y), _) in enumerate(pts):
        actions = []
        if pickup is not None and (x, y) == tuple(pickup):
            actions.append({"actionType": "pick", "actionId": f"{order_id}-pick", "blockingType": "HARD",
                            "actionParameters": [{"key": "load", "value": "kit"}]})
        if i == len(pts) - 1:
            actions.append({"actionType": "drop", "actionId": f"{order_id}-drop", "blockingType": "HARD",
                            "actionParameters": [{"key": "load", "value": "kit"}]})
        nodes.append({"nodeId": f"c{x}_{y}", "sequenceId": 2 * i, "released": True,
                      "nodePosition": {"x": x * CELL_M, "y": y * CELL_M, "mapId": MAP_ID}, "actions": actions})
        if i:
            (px, py), pc = pts[i - 1]
            slow = crossing_speed_mps is not None and (pc or pts[i][1])
            edges.append({"edgeId": f"e{px}_{py}-{x}_{y}", "sequenceId": 2 * i - 1, "released": True,
                          "startNodeId": f"c{px}_{py}", "endNodeId": f"c{x}_{y}",
                          "maxSpeed": crossing_speed_mps if slow else speed_mps, "actions": []})
    return {"headerId": header_id, "timestamp": _now(), "version": VERSION, "manufacturer": MANUFACTURER,
            "serialNumber": _serial(agv_id), "orderId": order_id, "orderUpdateId": update_id,
            "nodes": nodes, "edges": edges}


def state(agent, header_id: int, last: dict | None = None, battery_pct: float | None = None) -> dict:
    x, y = agent.pos
    errors = [{"errorType": "vehicleFault", "errorLevel": "FATAL", "errorDescription": "stopped by a fault"}] \
        if agent.status == "fault" else [{"errorType": "blocked", "errorLevel": "WARNING",
                                          "errorDescription": "path blocked: stalled"}] if agent.status == "stalled" else []
    return {"headerId": header_id, "timestamp": _now(), "version": VERSION, "manufacturer": MANUFACTURER,
            "serialNumber": _serial(agent.id), "orderId": (last or {}).get("orderId", ""),
            "orderUpdateId": (last or {}).get("orderUpdateId", 0), "lastNodeId": f"c{x}_{y}",
            "lastNodeSequenceId": 0, "driving": agent.status == "moving",
            "operatingMode": "AUTOMATIC" if agent.status != "held" else "SEMIAUTOMATIC",
            "nodeStates": [], "edgeStates": [], "actionStates": [],
            "agvPosition": {"x": x * CELL_M, "y": y * CELL_M, "theta": 0.0, "mapId": MAP_ID, "positionInitialized": True},
            "batteryState": {"batteryCharge": float(battery_pct) if battery_pct is not None else 100.0, "charging": False},
            "errors": errors, "safetyState": {"eStop": "NONE", "fieldViolation": False}}


class Bridge:
    """Builds and keeps the messages: the last orders for the UI, one JSONL line each for the record."""

    def __init__(self, path: Path | None = None, keep: int = 50):
        self.path, self.orders = path, deque(maxlen=keep)
        self.last: dict[str, dict] = {}  # serial -> its latest order
        self.updates: dict[str, int] = {}
        self._ids = itertools.count(1)
        self.lock = threading.Lock()

    def publish_order(self, agv_id, path, speed_mps, crossing_speed_mps, key=None, grid=None, pickup=None) -> dict:
        with self.lock:
            order_id = (key or f"order-{next(self._ids)}").split(":")[0]
            n = self.updates[order_id] = self.updates.get(order_id, -1) + 1
            msg = order(agv_id, path, speed_mps, crossing_speed_mps, order_id=order_id, update_id=n,
                        header_id=next(self._ids), grid=grid, pickup=pickup)
            self.orders.append(msg)
            self.last[msg["serialNumber"]] = msg
            if self.path:
                try:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    with self.path.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps({"topic": f"uagv/v2/{MANUFACTURER}/{msg['serialNumber']}/order",
                                             "message": msg}) + "\n")
                except OSError:
                    pass
        return msg

    def states(self, sim, plant=None) -> list[dict]:
        out = []
        for a in sim.agents.values():
            if a.kind != "agv":
                continue
            e = plant.entity(a.id) if plant is not None else None
            out.append(state(a, next(self._ids), self.last.get(_serial(a.id)),
                             (e or {}).get("attrs", {}).get("battery_pct")))
        return out


class Fleet:
    """FleetManager that sends every AGV dispatch as a VDA 5050 order, then lets the inner fleet (the sim) move it."""

    def __init__(self, inner, bridge: Bridge, sim):
        self.inner, self.bridge, self.sim = inner, bridge, sim

    def dispatch(self, agent, goal, speed_mps=1.0, crossing_speed_mps=None, via=None, token=None, key=None):
        path = self.inner.dispatch(agent, goal, speed_mps, crossing_speed_mps, via=via, token=token, key=key)
        a = self.sim.agents.get(agent)
        if path and a is not None and a.kind == "agv":
            self.bridge.publish_order(agent, path, speed_mps, crossing_speed_mps, key=key, grid=self.sim.grid,
                                      pickup=self.sim.grid.nearest_walkable(*via) if via else None)
        return path
