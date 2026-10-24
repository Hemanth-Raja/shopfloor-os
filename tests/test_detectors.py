"""Adaptive Sentinel thresholds: history baselines, EWMA, CUSUM drift, fixed hard limits, stock trend, cooldown."""

import random

from app.autonomy.detectors import CUSUM_H, Detectors, Stream, StockTrend, load_baselines
from app.autonomy.sentinel import Sentinel


def test_baselines_come_from_history_per_machine_and_shift():
    from app.investigate.sources import offline_run_sql
    b = load_baselines(offline_run_sql)
    mean, sd = b[("M-06", "B")]
    assert 300 < mean < 320 and 0.3 <= sd < 5 and ("M-06", "A") in b and ("M-06", "N") in b


def test_cusum_catches_a_slow_drift_before_any_reading_is_abnormal():
    rng = random.Random(1)
    s = Stream(310.0, 1.0)
    for _ in range(30):  # normal: no alarm
        assert s.update(rng.gauss(310, 0.8))["drift"] is None
    fired = None
    for i in range(1, 40):  # +0.3 sd per reading
        d = s.update(310 + 0.3 * i + rng.gauss(0, 0.3))
        if d["drift"]:
            fired = (i, d["z"])
            break
    assert fired and fired[1] < 3.0  # flagged while the single reading is still under 3 sd


def test_the_baseline_does_not_learn_an_anomaly_and_hard_limits_never_adapt():
    det = Detectors({("M-01", "B"): (309.0, 1.0)})
    for _ in range(20):
        det.temperature("M-01", "B", 316.0)  # a long hot spell
    s = det.streams[("M-01", "B")]
    assert s.mean < 310 and det.hard_limit_k("M-01", "B") == 313.0  # max(313, 309 + 4 sd)
    assert det.temperature("M-01", "B", 313.5)["hard_limit"]
    assert Detectors({("M-06", "B"): (311.5, 1.2)}).hard_limit_k("M-06", "B") == 316.3  # a hotter machine


def test_stock_trend_raises_before_the_floor():
    t = StockTrend()
    outs = [t.update(m) for m in (12.0, 10.9, 9.8, 8.7, 7.6, 6.5, 5.4)]
    assert not any(o["early"] for o in outs[:-2]) and outs[-1]["early"]
    assert t.update(14.0)["rate"] == 0.0  # replenished: the trend resets


def test_sentinel_uses_detectors_and_holds_a_persisting_condition_after_it_is_handled():
    made = []

    def raise_incident(spec):
        made.append(spec)
        from types import SimpleNamespace
        return SimpleNamespace(id=f"I{len(made)}")
    s = Sentinel(log_path=None, detectors=Detectors({("M-06", "B"): (310.0, 1.0)}))
    sig = lambda t, v: {"type": "machine_telemetry", "machine": "M-06", "station": "F10", "process_temp_k": v,  # noqa: E731
                        "telemetry": True, "tick": t}
    assert s.process(sig(1, 310.2), 1, raise_incident) is None
    assert s.process(sig(2, 313.6), 2, raise_incident) is not None  # 3.6 sd: raised
    s.close("I1", 3)
    assert s.process(sig(4, 313.4), 4, raise_incident) is None  # same condition, just handled: cooldown
    assert s.process(sig(5, 315.2), 5, raise_incident) is not None  # over the hard limit: re-raised at once
    assert [m["kind"] for m in made] == ["overheat", "overheat"] and made[1]["severity"] == "high"
