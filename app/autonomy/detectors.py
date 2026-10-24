"""Adaptive detection for the Sentinel: EWMA baselines plus CUSUM drift detection, seeded from history. No models.

For each stream (e.g. process temperature of machine M-06 on shift B):
- the baseline mean and spread start from history: sf_plant.machine_telemetry, per machine and shift, through
  Zetaris (or the offline copy), and then follow the stream by EWMA (lambda 0.1), but only on normal readings, so an
  anomaly never teaches the baseline that it is normal;
- z = (x - mean) / sd: |z| >= 3 is a clear anomaly, 2 <= |z| < 3 is ambiguous (triage);
- a two-sided CUSUM (k = 0.5 sd, h = 5 sd) accumulates small, persistent shifts: it fires on a slow drift long before
  any single reading crosses a fixed threshold.

Safety floors are fixed and never adapt: a temperature above the machine's hard limit is always at least an alarm,
and person-safety signals (proximity, spill camera) keep fixed limits. Adaptation can only add sensitivity.

Line-side stock uses a trend detector: EWMA of the consumption rate projects when a station will run dry, so the
Sentinel raises the shortage before the hard 5-minute floor is reached.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

LAMBDA, Z_CLEAR, Z_AMBIG, CUSUM_K, CUSUM_H = 0.1, 3.0, 2.0, 0.5, 5.0
HARD_TEMP_K = 313.0  # the lowest hard limit; per machine it is max(313 K, historical mean + 4 sd) for the shift
HARD_SD = 4.0
STOCK_FLOOR_MIN, STOCK_HORIZON = 5.0, 4


def shift_of_hour(h: int) -> str:
    return "A" if 6 <= h < 14 else "B" if 14 <= h < 22 else "N"


def load_baselines(run_sql) -> dict[tuple[str, str], tuple[float, float]]:
    """(machine, shift) -> (mean, sd) of process_temp_k from history; {} when the data is not reachable."""
    from app.investigate.sqlguard import check
    sql = check("SELECT machine_id, CASE WHEN hour(ts) >= 6 AND hour(ts) < 14 THEN 'A' WHEN hour(ts) >= 14 AND "
                "hour(ts) < 22 THEN 'B' ELSE 'N' END AS shift_code, avg(process_temp_k) AS mean_k, "
                "avg(process_temp_k * process_temp_k) AS mean_sq FROM sf_plant.machine_telemetry GROUP BY 1, 2")
    try:
        rows = run_sql(sql)
    except Exception:  # noqa: BLE001 - fall back to the stream's own first readings
        return {}
    out = {}
    for m, s, mean, sq in rows:
        mean, sq = float(mean), float(sq)
        out[(str(m), str(s))] = (mean, max(0.3, math.sqrt(max(0.0, sq - mean * mean))))
    return out


@dataclass
class Stream:
    mean: float
    sd: float
    pos: float = 0.0  # CUSUM upward
    neg: float = 0.0  # CUSUM downward
    n: int = 0

    def update(self, x: float) -> dict:
        z = (x - self.mean) / self.sd
        self.pos = max(0.0, self.pos + z - CUSUM_K)
        self.neg = max(0.0, self.neg - z - CUSUM_K)
        drift = "up" if self.pos >= CUSUM_H else "down" if self.neg >= CUSUM_H else None
        level = "clear" if abs(z) >= Z_CLEAR else "ambiguous" if abs(z) >= Z_AMBIG else "normal"
        quiet = self.pos < CUSUM_H / 2 and self.neg < CUSUM_H / 2
        if level == "normal" and not drift and quiet:  # learn only from normal readings, never during a building drift
            d = x - self.mean
            self.mean += LAMBDA * d
            self.sd = max(0.3, math.sqrt((1 - LAMBDA) * (self.sd ** 2 + LAMBDA * d * d)))
        if drift:
            self.pos = self.neg = 0.0  # report a drift once, then start accumulating again
        self.n += 1
        return {"z": round(z, 2), "level": level, "drift": drift, "mean": round(self.mean, 2), "sd": round(self.sd, 2)}


@dataclass
class StockTrend:
    rate: float | None = None  # minutes of stock lost per reading (EWMA)
    last: float | None = None

    def update(self, minutes_left: float) -> dict:
        if self.last is not None and minutes_left <= self.last:
            used = self.last - minutes_left
            self.rate = used if self.rate is None else (1 - 0.3) * self.rate + 0.3 * used
        elif self.last is not None and minutes_left > self.last:
            self.rate = None  # replenished: start a new trend
        self.last = minutes_left
        projected = minutes_left - (self.rate or 0.0) * STOCK_HORIZON
        return {"minutes_left": minutes_left, "rate": round(self.rate or 0.0, 2), "projected": round(projected, 1),
                "early": minutes_left >= STOCK_FLOOR_MIN and projected < 2.0}


@dataclass
class Detectors:
    baselines: dict = field(default_factory=dict)
    streams: dict = field(default_factory=dict)
    stock: dict = field(default_factory=dict)

    def hard_limit_k(self, machine: str, shift: str) -> float:
        """Fixed engineering limit from history (never adapted by the stream): mean + 4 sd, at least 313 K."""
        mean, sd = self.baselines.get((machine, shift), (HARD_TEMP_K - HARD_SD, 1.0))
        return round(max(HARD_TEMP_K, mean + HARD_SD * sd), 2)

    def temperature(self, machine: str, shift: str, value_k: float) -> dict:
        key = (machine, shift)
        if key not in self.streams:
            mean, sd = self.baselines.get(key, (value_k, 1.0))
            self.streams[key] = Stream(mean, sd)
        out = self.streams[key].update(value_k)
        out["limit_k"] = self.hard_limit_k(machine, shift)
        out["hard_limit"] = value_k > out["limit_k"]
        return out

    def stock_level(self, station: str, minutes_left: float) -> dict:
        return self.stock.setdefault(station, StockTrend()).update(minutes_left)
