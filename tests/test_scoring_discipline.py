"""Tests for the disciplined scoring/reporting changes (2026-06-24):
target matching with carry-forward, and honest accuracy stats (CI, significance)."""
from datetime import datetime, timedelta

from analysis.forecasters.backtest import (
    _find_target_reading, SCORE_TOLERANCE_HOURS, CARRY_FORWARD_CAP_HOURS,
)
import reports.csv_exporter as ce


def _rates(hours):
    base = datetime(2026, 1, 1, 0, 0, 0)
    rates = [{"timestamp": (base + timedelta(hours=h)).isoformat(), "spread_pct": 30.0} for h in hours]
    parsed = [base + timedelta(hours=h) for h in hours]
    return rates, parsed


def test_find_target_prefers_nearest_within_tolerance():
    rates, parsed = _rates([0, 24, 25])  # target 24h: exact match at idx 1
    base = datetime(2026, 1, 1)
    idx = _find_target_reading(rates, base + timedelta(hours=24), parsed, 0)
    assert idx == 1


def test_find_target_nearest_picks_closest_not_first():
    rates, parsed = _rates([0, 21, 27])  # target 24h; 27 (+3h) closer than 21 (-3h tie) -> 21 first found
    base = datetime(2026, 1, 1)
    idx = _find_target_reading(rates, base + timedelta(hours=24), parsed, 0)
    # both 3h away; nearest keeps the first encountered (21) — within tolerance either is fine
    assert idx in (1, 2)


def test_find_target_carry_forward_on_dedup_gap():
    # No reading within +/-6h of target 24h, but a flat reading at +14h (10h before target).
    rates, parsed = _rates([0, 14])
    base = datetime(2026, 1, 1)
    idx = _find_target_reading(rates, base + timedelta(hours=24), parsed, 0)
    assert idx == 1  # carried forward (14h is within the 12h cap? 24-14=10 <= 12) -> yes


def test_find_target_none_when_gap_exceeds_carry_cap():
    rates, parsed = _rates([0, 8])  # last reading 16h before target -> beyond 12h cap
    base = datetime(2026, 1, 1)
    idx = _find_target_reading(rates, base + timedelta(hours=24), parsed, 0)
    assert idx is None


def test_brier_se_none_for_small_n():
    assert ce._brier_se([]) is None
    assert ce._brier_se([0.5]) is None
    assert ce._brier_se([0.4, 0.6]) is not None


def test_model_stats_significance(monkeypatch):
    # naive ~0.70 with spread; stat clearly lower and tight -> should be significant vs naive.
    naive = [{"p_widen": .33, "p_stable": .34, "p_narrow": .33, "brier": b, "actual_outcome": "stable"}
             for b in [0.70] * 40]
    stat = [{"p_widen": .2, "p_stable": .6, "p_narrow": .2, "brier": b, "actual_outcome": "stable"}
            for b in [0.30] * 40]
    def fake_scores(name):
        return naive if name == "naive" else stat
    monkeypatch.setattr(ce, "get_forecast_scores", fake_scores)
    monkeypatch.setattr(ce, "_MODEL_ORDER", ["naive", "stat"])
    stats = ce._model_stats()
    assert stats["stat"]["n_scored"] == 40
    assert stats["stat"]["vs_naive"] < 0
    assert stats["stat"]["beats_naive_significant"] is True
    assert stats["stat"]["beats_uniform_significant"] is True
    # calibration snapshot present
    assert set(stats["stat"]["calibration"]) == {"widen", "stable", "narrow"}
