"""Lab Level 2: hypothesis -> seeded replays (no LLM) -> bootstrap CI -> experiment proposal.

1. Hypothesis (Nemotron, via app/llm.py). From aggregated incident history (one Zetaris query), the model
   proposes ONE policy change from the menu the replay can test:
   - move_spill_kit {"station": id}: place an extra spill kit at a station
   - preposition_agv {"station": id}: keep a loaded kitting AGV staged at a station's line side
   If Nightshift is unavailable, a rule-based hypothesis (the station with the most incidents) is used.
2. Replays, deterministic and with no LLM (AGENTS.md rule 5). The historical incidents of that kind are
   re-scored under baseline and treatment. Walking distances use the sim's A* on the real floor grid.
   Each incident keeps its own residual (actual minus the modelled travel term), so only the policy's
   effect on travel changes. 20 seeded replays bootstrap-resample the incidents and add the same jitter
   to both arms.
3. Statistics: the paired per-incident differences give a bootstrap 95% CI (2,000 resamples, seeded) for
   the change in mean time to clear.
4. Proposal: a row for ops.experiments with status "proposed". It is written only to the local Postgres
   container; Zetaris stays read-only.
"""

from __future__ import annotations

import json
import random
import statistics
import subprocess
from datetime import datetime
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.data import generate as g
from app.sim.grid import Grid
from app.sim.pathing import plan_path

KIND = {"move_spill_kit": "fluid_spill", "preposition_agv": "part_shortage"}
SPILL_SECONDS_PER_CELL = 12  # fetching and carrying the kit (two trips)
SHORTAGE_SECONDS_PER_CELL = 10  # supermarket round trip per cell
STAGED_DISTANCE = 2  # a staged AGV waits two cells from the line side
REPLAYS = 20
BOOTSTRAP = 2000

HISTORY_SQL = """
SELECT incident_id, kind, cause, station_id, zone_id, x, y, time_to_clear_s, spill_kit_distance, supermarket_distance,
       shift_code, false_alarm
FROM sf_ops.incidents
WHERE kind = '{kind}' AND NOT false_alarm
"""
SUMMARY_SQL = """
SELECT kind, station_id, zone_id, count(*) AS n, round(avg(time_to_clear_s)) AS avg_clear_s,
       round(avg(downtime_min), 1) AS avg_downtime_min, round(avg(spill_kit_distance), 1) AS avg_kit_cells,
       round(avg(supermarket_distance), 1) AS avg_smkt_cells
FROM sf_ops.incidents
WHERE kind IN ('fluid_spill', 'part_shortage') AND NOT false_alarm
GROUP BY kind, station_id, zone_id
ORDER BY n DESC
LIMIT 12
"""


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["move_spill_kit", "preposition_agv"]
    station: str


class Hypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hypothesis: str = Field(min_length=10, max_length=400)
    policy: Policy
    metric: Literal["time_to_clear_s"] = "time_to_clear_s"
    expected_effect_pct: float = Field(ge=-100, le=100)


def propose(summary: list[list], llm_json: Callable | None) -> tuple[Hypothesis, str]:
    rows = [dict(zip(["kind", "station_id", "zone_id", "n", "avg_clear_s", "avg_downtime_min", "avg_kit_cells",
                      "avg_smkt_cells"], r)) for r in summary]
    if llm_json is not None:
        try:
            reply = llm_json("deep", [
                {"role": "system", "content": "You are an industrial engineer. Propose ONE testable policy change that "
                 "would cut mean time_to_clear_s. Allowed policies: move_spill_kit (add a spill kit at a station; helps "
                 "fluid_spill) or preposition_agv (stage a loaded kitting AGV at a station; helps part_shortage). "
                 f"Spill kits today: {[k[0] + '@' + str(k[1:]) for k in g.SPILL_KITS]}; the supermarket exit is "
                 "at (7, 13). Use a station id from the data."},
                {"role": "user", "content": json.dumps(rows)}], Hypothesis, timeout_s=120)
            return (reply.data if hasattr(reply, "data") else reply), "nemotron"
        except Exception:  # noqa: BLE001 - fall back to a rule-based hypothesis
            pass
    top = max((r for r in rows if r["kind"] == "fluid_spill" and r["station_id"]), key=lambda r: r["n"], default=None)
    station = top["station_id"] if top else "F20"
    return Hypothesis(hypothesis=f"An extra spill kit at {station} cuts spill clean-up time there.",
                      policy=Policy(type="move_spill_kit", station=station), expected_effect_pct=-20), "rule-based"


def _walk(grid: Grid, a: tuple[int, int], b: tuple[int, int], cache: dict) -> int:
    key = (a, b)
    if key not in cache:
        path = plan_path(grid, grid.nearest_walkable(*a), grid.nearest_walkable(*b), 0, max_ticks=300)
        cache[key] = len(path) - 1 if path else abs(a[0] - b[0]) + abs(a[1] - b[1])
    return cache[key]


def rescore(rows: list[dict], policy: Policy, grid: Grid) -> list[tuple[float, float]]:
    """(baseline, treatment) time to clear for each historical incident. No LLM, fully deterministic."""
    station = {s[0]: s for s in g.STATIONS}[policy.station]
    staged = (station[3], station[4] + 2)
    cache: dict = {}
    pairs = []
    for r in rows:
        actual = float(r["time_to_clear_s"])
        cell = (int(r["x"]), int(r["y"]))
        if policy.type == "move_spill_kit":
            old = min(_walk(grid, (kx, ky), cell, cache) for _, kx, ky in g.SPILL_KITS)
            new = min(old, _walk(grid, staged, cell, cache))
            pairs.append((actual, actual + SPILL_SECONDS_PER_CELL * (new - old)))
        else:
            helped = r["station_id"] == policy.station and r["cause"] in ("agv_late", "agv_down")
            old = _walk(grid, g.SMKT_EXIT, cell, cache)
            new = min(old, STAGED_DISTANCE) if helped else old
            pairs.append((actual, actual + SHORTAGE_SECONDS_PER_CELL * (new - old)))
    return pairs


def replays(pairs: list[tuple[float, float]], n: int = REPLAYS, seed: int = 42) -> list[dict]:
    """n seeded replays: resample incidents, add the same jitter to both arms."""
    out = []
    for i in range(n):
        rng = random.Random(seed + i)
        sample = [pairs[rng.randrange(len(pairs))] for _ in pairs]
        jitter = [rng.gauss(0, 30) for _ in sample]
        base = statistics.fmean(b + j for (b, _), j in zip(sample, jitter))
        treat = statistics.fmean(max(60.0, t + j) for (_, t), j in zip(sample, jitter))
        out.append({"seed": seed + i, "baseline_mean_s": round(base, 1), "treatment_mean_s": round(treat, 1)})
    return out


def bootstrap_ci(pairs: list[tuple[float, float]], n: int = BOOTSTRAP, seed: int = 7) -> tuple[float, float, float]:
    """Mean paired difference (treatment - baseline) with a percentile 95% CI."""
    diffs = [t - b for b, t in pairs]
    rng = random.Random(seed)
    means = sorted(statistics.fmean(diffs[rng.randrange(len(diffs))] for _ in diffs) for _ in range(n))
    return statistics.fmean(diffs), means[int(0.025 * n)], means[int(0.975 * n) - 1]


def run_experiment(run_sql: Callable, llm_json: Callable | None, grid: Grid | None = None) -> dict:
    grid = grid or Grid.load(run_sql)
    hyp, source = propose(run_sql(SUMMARY_SQL), llm_json)
    cols = ["incident_id", "kind", "cause", "station_id", "zone_id", "x", "y", "time_to_clear_s", "spill_kit_distance",
            "supermarket_distance", "shift_code", "false_alarm"]
    rows = [dict(zip(cols, r)) for r in run_sql(HISTORY_SQL.format(kind=KIND[hyp.policy.type]))]
    pairs = rescore(rows, hyp.policy, grid)
    reps = replays(pairs)
    mean_diff, lo, hi = bootstrap_ci(pairs)
    base = statistics.fmean(b for b, _ in pairs)
    effect = 100 * mean_diff / base
    significant = hi < 0
    return {"hypothesis": hyp.model_dump(), "source": source, "incidents": len(pairs),
            "baseline_mean_s": round(base, 1), "treatment_mean_s": round(base + mean_diff, 1),
            "effect_pct": round(effect, 1), "ci_low_s": round(lo, 1), "ci_high_s": round(hi, 1),
            "ci_low_pct": round(100 * lo / base, 1), "ci_high_pct": round(100 * hi / base, 1),
            "replays": reps, "significant": significant,
            "status": "proposed" if significant else "inconclusive"}


def experiment_row(result: dict) -> dict:
    h = result["hypothesis"]
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    return {"experiment_id": f"EXP-{stamp}", "created_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
            "hypothesis": h["hypothesis"], "policy": json.dumps(h["policy"]),
            "baseline_mean_s": result["baseline_mean_s"], "treatment_mean_s": result["treatment_mean_s"],
            "effect_pct": result["effect_pct"], "ci_low": result["ci_low_pct"], "ci_high": result["ci_high_pct"],
            "status": result["status"]}


def _lit(v) -> str:
    if isinstance(v, (int, float)):
        return str(v)
    return "$exp$" + str(v).replace("$exp$", "") + "$exp$"  # dollar quoting: no escaping problems


def write_local(row: dict, container: str = "shopfloor-pg") -> str:
    """Insert the proposal into the LOCAL Postgres (ops.experiments). Zetaris stays read-only."""
    cols = list(row)
    sql = f"INSERT INTO experiments ({', '.join(cols)}) VALUES ({', '.join(_lit(row[c]) for c in cols)})"
    out = subprocess.run(["docker", "exec", "-i", container, "psql", "-U", "postgres", "-d", "ops", "-v",
                          "ON_ERROR_STOP=1", "-q"], input=sql, capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip()[:300])
    return row["experiment_id"]
