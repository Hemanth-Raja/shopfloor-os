"""The 20 x 40 floor grid, from sf_ops.floor_cells (cached in data/cache/ after the first load)."""

from __future__ import annotations

import json
from pathlib import Path

CACHE = Path(__file__).resolve().parents[2] / "data" / "cache" / "floor_cells.json"
BLOCKED_KINDS = {"rack", "conveyor", "robot_cell", "paint_booth"}
CROSSINGS = {"X-WEST", "X-EAST"}
CELL_M = 2.0


class Grid:
    def __init__(self, cells: dict[tuple[int, int], str]):
        self.cells = cells
        self.cols = 1 + max(x for x, _ in cells)
        self.rows = 1 + max(y for _, y in cells)

    def label(self, x: int, y: int) -> str | None:
        return self.cells.get((x, y))

    def walkable(self, x: int, y: int) -> bool:
        label = self.cells.get((x, y))
        return label is not None and label not in BLOCKED_KINDS

    def is_crossing(self, x: int, y: int) -> bool:
        return self.cells.get((x, y)) in CROSSINGS

    def neighbours(self, x: int, y: int) -> list[tuple[int, int]]:
        return [(nx, ny) for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)) if self.walkable(nx, ny)]

    def nearest_walkable(self, x: int, y: int) -> tuple[int, int]:
        if self.walkable(x, y):
            return x, y
        best = min((c for c in self.cells if self.walkable(*c)), key=lambda c: (abs(c[0] - x) + abs(c[1] - y), c))
        return best

    def cells_within(self, x: int, y: int, radius: int) -> set[tuple[int, int]]:
        return {(cx, cy) for (cx, cy) in self.cells if abs(cx - x) + abs(cy - y) <= radius}

    @classmethod
    def from_rows(cls, rows: list[list]) -> "Grid":
        return cls({(int(r[0]), int(r[1])): str(r[2]) for r in rows})

    @classmethod
    def from_generator(cls) -> "Grid":
        """Offline: the same cells generate.py writes to sf_ops.floor_cells."""
        from app.data import generate as g
        return cls({(x, y): g.BLOCKED.get((x, y)) or g.zone_of(x, y) for y in range(g.ROWS) for x in range(g.COLS)})

    @classmethod
    def load(cls, run_sql=None) -> "Grid":
        """Cache first; otherwise one read-only Zetaris query, then cache it under data/cache/."""
        if CACHE.exists():
            return cls.from_rows(json.loads(CACHE.read_text()))
        if run_sql is None:
            return cls.from_generator()
        rows = run_sql("SELECT x, y, cell FROM sf_ops.floor_cells ORDER BY y, x")
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(rows))
        return cls.from_rows(rows)
