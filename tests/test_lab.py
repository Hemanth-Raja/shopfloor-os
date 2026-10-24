"""Lab Level 2: deterministic replays, bootstrap CI and the experiment proposal, offline."""

from types import SimpleNamespace

from app.lab.experiment import (Hypothesis, Policy, _lit, bootstrap_ci, experiment_row, propose, replays, rescore,
                                run_experiment)

SPILLS = [{"incident_id": f"INC-{i}", "kind": "fluid_spill", "cause": "coolant", "station_id": "F20", "zone_id": "FINAL",
           "x": 28, "y": 9, "time_to_clear_s": 700 + 10 * i} for i in range(12)]
SHORT = [{"incident_id": f"S-{i}", "kind": "part_shortage", "cause": "agv_late" if i % 2 else "mispick",
          "station_id": "F10", "zone_id": "FINAL", "x": 25, "y": 9, "time_to_clear_s": 900} for i in range(10)]


def test_rescore_is_deterministic_and_never_slower(grid):
    pairs = rescore(SPILLS, Policy(type="move_spill_kit", station="F20"), grid)
    assert pairs == rescore(SPILLS, Policy(type="move_spill_kit", station="F20"), grid)
    assert all(t < b for b, t in pairs)  # the kit at F20 is closer than the dock and the rework bay


def test_preposition_helps_only_agv_causes_at_that_station(grid):
    pairs = rescore(SHORT, Policy(type="preposition_agv", station="F10"), grid)
    helped = [t < b for (b, t), r in zip(pairs, SHORT)]
    assert helped == [r["cause"] == "agv_late" for r in SHORT]


def test_replays_and_ci_are_seeded():
    pairs = [(800.0, 700.0 + i) for i in range(30)]
    assert replays(pairs) == replays(pairs) and len(replays(pairs)) == 20
    mean, lo, hi = bootstrap_ci(pairs)
    assert lo <= mean <= hi < 0


def test_rule_based_hypothesis_when_nightshift_is_down():
    def down(*a, **k):
        raise TimeoutError
    hyp, source = propose([["fluid_spill", "F20", "FINAL", 40, 500, 1.0, 20, None]], down)
    assert source == "rule-based" and hyp.policy == Policy(type="move_spill_kit", station="F20")


def test_run_experiment_offline_with_fake_model(grid):
    def fake_sql(sql):
        if "GROUP BY" in sql:
            return [["fluid_spill", "F20", "FINAL", 12, 760, 1.0, 22, None]]
        return [[r[c] for c in ("incident_id", "kind", "cause", "station_id", "zone_id", "x", "y", "time_to_clear_s")]
                + [None, None, "A", False] for r in SPILLS]
    hyp = Hypothesis(hypothesis="A kit at F20 shortens spill clean-up.", policy=Policy(type="move_spill_kit", station="F20"),
                     expected_effect_pct=-15)
    result = run_experiment(fake_sql, lambda *a, **k: SimpleNamespace(data=hyp), grid)
    assert result["source"] == "nemotron" and result["significant"] and result["status"] == "proposed"
    assert result["ci_low_pct"] <= result["effect_pct"] <= result["ci_high_pct"] < 0
    row = experiment_row(result)
    assert row["status"] == "proposed" and row["experiment_id"].startswith("EXP-")


def test_sql_literals_are_safe():
    assert _lit("it's $exp$ ok") == "$exp$it's  ok$exp$" and _lit(-7.5) == "-7.5"


def test_zetaris_write_is_one_insert_then_a_check():
    from app.lab.experiment import _spark_lit, write_zetaris
    calls = []

    def fake_sql(sql):
        calls.append(sql)
        return [["EXP-1"]] if sql.startswith("SELECT") else []
    row = {"experiment_id": "EXP-1", "created_at": "2026-10-24 06:00:00", "hypothesis": "AGV's kit", "policy": "{}",
           "baseline_mean_s": 800.0, "treatment_mean_s": 740.0, "effect_pct": -7.5, "ci_low": -8.0, "ci_high": -6.0,
           "status": "proposed"}
    assert write_zetaris(row, fake_sql) == "EXP-1"
    assert calls[0].startswith("INSERT INTO sf_ops.experiments") and "CAST('2026-10-24 06:00:00' AS TIMESTAMP)" in calls[0]
    assert "'AGV\\'s kit'" in calls[0] and ";" not in calls[0] and len(calls) == 2
    assert _spark_lit("a\\b") == "'a\\\\b'"
