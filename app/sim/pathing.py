"""Space-time A* with a reservation table (cooperative path finding).

A path is a list of cells, one per tick, starting at the agent's cell at `start_tick`. The reservation
table holds (x, y, tick) vertex reservations and edge swaps, so no two agents share a cell in the same
tick and no two agents swap cells head-on. Agents may wait in place.
"""

from __future__ import annotations

import heapq
from itertools import count

from app.sim.grid import Grid

Cell = tuple[int, int]


class Reservations:
    def __init__(self):
        self.vertex: dict[tuple[int, int, int], str] = {}
        self.edges: set[tuple[Cell, Cell, int]] = set()
        self.parked: dict[Cell, str] = {}  # agents that stay put indefinitely

    def free(self, cell: Cell, tick: int, agent: str) -> bool:
        owner = self.vertex.get((cell[0], cell[1], tick))
        parked = self.parked.get(cell)
        return (owner in (None, agent)) and (parked in (None, agent))

    def reserve_path(self, agent: str, path: list[Cell], start_tick: int, park: bool = True) -> None:
        self.release(agent)
        for i, cell in enumerate(path):
            self.vertex[(cell[0], cell[1], start_tick + i)] = agent
            if i:
                self.edges.add((path[i - 1], cell, start_tick + i))
        if park and path:
            self.parked[path[-1]] = agent

    def park(self, agent: str, cell: Cell) -> None:
        self.release(agent)
        self.parked[cell] = agent

    def release(self, agent: str) -> None:
        for key in [k for k, v in self.vertex.items() if v == agent]:
            del self.vertex[key]
        for cell in [c for c, v in self.parked.items() if v == agent]:
            del self.parked[cell]

    def owners_at(self, tick: int) -> dict[Cell, str]:
        out = {c: a for c, a in self.parked.items()}
        out.update({(x, y): a for (x, y, t), a in self.vertex.items() if t == tick})
        return out


def plan_path(grid: Grid, start: Cell, goal: Cell, start_tick: int, reservations: Reservations | None = None,
              agent: str = "", blocked: set[Cell] | None = None, max_ticks: int = 150) -> list[Cell] | None:
    """Shortest collision-free path from start to goal; None if unreachable within max_ticks."""
    res = reservations or Reservations()
    blocked = blocked or set()
    if not grid.walkable(*goal) or goal in blocked:
        return None
    tie = count()
    h = lambda c: abs(c[0] - goal[0]) + abs(c[1] - goal[1])  # noqa: E731
    open_set = [(h(start), next(tie), start, start_tick)]
    came: dict[tuple[Cell, int], tuple[Cell, int] | None] = {(start, start_tick): None}
    while open_set:
        _, _, cell, t = heapq.heappop(open_set)
        if cell == goal and all(res.free(goal, t + k, agent) for k in range(1, 4)):
            path = [cell]
            node = (cell, t)
            while came[node] is not None:
                node = came[node]
                path.append(node[0])
            return path[::-1]
        if t - start_tick >= max_ticks:
            continue
        for nxt in grid.neighbours(*cell) + [cell]:  # move or wait
            nt = t + 1
            if nxt in blocked and nxt != start:
                continue
            if not res.free(nxt, nt, agent):
                continue
            if (nxt, cell, nt) in res.edges:  # head-on swap
                continue
            key = (nxt, nt)
            if key in came:
                continue
            came[key] = (cell, t)
            heapq.heappush(open_set, (nt - start_tick + h(nxt), next(tie), nxt, nt))
    return None


def path_cells_ok(grid: Grid, path: list[Cell]) -> bool:
    """Every step is a walkable cell and moves at most one cell (4-neighbourhood) or waits."""
    return all(grid.walkable(*c) for c in path) and all(
        abs(a[0] - b[0]) + abs(a[1] - b[1]) <= 1 for a, b in zip(path, path[1:]))
