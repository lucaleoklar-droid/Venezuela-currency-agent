"""Forecaster math and forecaster class smoke tests. All pure — no DB, no I/O."""
import math
import pytest
from datetime import date, datetime, timedelta

from analysis.forecasters import (
    OUTCOMES, STABILITY_BAND_PP,
    brier_score, compute_outcome, log_loss, uniform, validate_probs,
)
from analysis.forecasters.naive import NaiveForecaster
from analysis.forecasters.stat import StatForecaster
from analysis.forecasters.stat_v2 import StatV2Forecaster
from analysis.forecasters.stat_v3 import StatV3Forecaster
from analysis.forecasters.payday import payday_features


# ---------------------------------------------------------------------------
# Shared test helper
# ---------------------------------------------------------------------------

def _make_history(n_readings=60, base_spread=30.0, trend_pp=0.0, enriched=False):
    """Synthetic history: readings every 6h, optional linear trend.
    enriched=True adds brent and news fields needed by StatV2/V3."""
    rows = []
    base = datetime(2026, 1, 1, 0, 0, 0)
    for i in range(n_readings):
        t = base + timedelta(hours=i * 6)
        spread = base_spread + trend_pp * i
        parallel = 100.0 * (1 + spread / 100)
        row = {
            "timestamp": t.isoformat(),
            "bcv_rate": 100.0,
            "parallel_rate": parallel,
            "spread_pct": spread,
        }
        if enriched:
            row["brent_usd_per_bbl"] = 75.0
            row["brent_usd_per_bbl_7d_ago"] = 73.0
            row["news_count_7d"] = 0
        rows.append(row)
    return rows


def _is_valid_probs(p: dict) -> bool:
    return (
        abs(sum(p.values()) - 1.0) < 1e-9
        and all(0.0 <= v <= 1.0 for v in p.values())
        and set(p.keys()) == {"widen", "stable", "narrow"}
    )


# ---------------------------------------------------------------------------
# NaiveForecaster
# ---------------------------------------------------------------------------

class TestNaiveForecaster:
    def test_valid_probs_on_normal_history(self):
        p = NaiveForecaster().forecast(_make_history(60))
        assert _is_valid_probs(p)

    def test_empty_history_returns_uniform(self):
        assert NaiveForecaster().forecast([]) == uniform()

    def test_single_reading_returns_uniform(self):
        assert NaiveForecaster().forecast(_make_history(1)) == uniform()

    def test_stable_spread_favours_stable(self):
        # Flat spread → all outcomes are stable → Laplace-smoothed stable dominant
        p = NaiveForecaster().forecast(_make_history(60, trend_pp=0.0))
        assert p["stable"] >= p["widen"]
        assert p["stable"] >= p["narrow"]

    def test_rising_spread_favours_widen(self):
        # Spread grows 0.5pp per reading → most 24h outcomes are widen
        p = NaiveForecaster().forecast(_make_history(60, trend_pp=0.5))
        assert p["widen"] > p["stable"]
        assert p["widen"] > p["narrow"]


# ---------------------------------------------------------------------------
# StatForecaster
# ---------------------------------------------------------------------------

class TestStatForecaster:
    def test_valid_probs_on_normal_history(self):
        p = StatForecaster().forecast(_make_history(60))
        assert _is_valid_probs(p)

    def test_empty_history_returns_uniform(self):
        assert StatForecaster().forecast([]) == uniform()

    def test_too_few_examples_returns_uniform(self):
        # min_examples default is 5; 4 readings can't produce 5 non-overlapping
        # 6h-decimated training points with a 24h-horizon outcome each
        p = StatForecaster().forecast(_make_history(4))
        assert p == uniform()

    def test_missing_spread_in_history_returns_uniform(self):
        rows = _make_history(60)
        for r in rows:
            r["spread_pct"] = None
        assert StatForecaster().forecast(rows) == uniform()


# ---------------------------------------------------------------------------
# StatV2Forecaster
# ---------------------------------------------------------------------------

class TestStatV2Forecaster:
    def test_valid_probs_with_enriched_history(self):
        p = StatV2Forecaster().forecast(_make_history(60, enriched=True))
        assert _is_valid_probs(p)

    def test_missing_brent_returns_uniform(self):
        # Without brent fields _features_at returns None for every row
        p = StatV2Forecaster().forecast(_make_history(60, enriched=False))
        assert p == uniform()

    def test_zero_brent_prev_returns_uniform(self):
        rows = _make_history(60, enriched=True)
        for r in rows:
            r["brent_usd_per_bbl_7d_ago"] = 0.0
        assert StatV2Forecaster().forecast(rows) == uniform()

    def test_empty_history_returns_uniform(self):
        assert StatV2Forecaster().forecast([]) == uniform()

    def test_none_brent_returns_uniform(self):
        rows = _make_history(60, enriched=True)
        for r in rows:
            r["brent_usd_per_bbl"] = None
        assert StatV2Forecaster().forecast(rows) == uniform()


# ---------------------------------------------------------------------------
# StatV3Forecaster
# ---------------------------------------------------------------------------

class TestStatV3Forecaster:
    def test_valid_probs_with_enriched_history(self):
        p = StatV3Forecaster().forecast(_make_history(60, enriched=True))
        assert _is_valid_probs(p)

    def test_missing_brent_returns_uniform(self):
        p = StatV3Forecaster().forecast(_make_history(60, enriched=False))
        assert p == uniform()

    def test_empty_history_returns_uniform(self):
        assert StatV3Forecaster().forecast([]) == uniform()

    def test_output_independent_of_v2_on_same_data(self):
        # V3 adds payday features — its output must still be valid even if it
        # differs from V2. We don't assert they must differ (kernel may converge
        # to same answer on synthetic flat data) but both must be valid.
        history = _make_history(60, enriched=True)
        assert _is_valid_probs(StatV2Forecaster().forecast(history))
        assert _is_valid_probs(StatV3Forecaster().forecast(history))


# ---------------------------------------------------------------------------
# compute_outcome — the frozen ±1pp band
# ---------------------------------------------------------------------------

class TestComputeOutcome:
    def test_widen_above_band(self):
        assert compute_outcome(30.0, 31.5) == "widen"

    def test_stable_inside_band(self):
        assert compute_outcome(30.0, 30.5) == "stable"
        assert compute_outcome(30.0, 29.5) == "stable"

    def test_narrow_below_band(self):
        assert compute_outcome(30.0, 28.0) == "narrow"

    def test_exactly_at_band_edge_is_stable(self):
        # ±exactly STABILITY_BAND_PP is *not* >; falls into stable
        assert compute_outcome(30.0, 30.0 + STABILITY_BAND_PP) == "stable"
        assert compute_outcome(30.0, 30.0 - STABILITY_BAND_PP) == "stable"


# ---------------------------------------------------------------------------
# validate_probs — clamp + renormalize
# ---------------------------------------------------------------------------

class TestValidateProbs:
    def test_already_valid(self):
        p = {"widen": 0.4, "stable": 0.3, "narrow": 0.3}
        out = validate_probs(p)
        assert pytest.approx(sum(out.values()), abs=1e-9) == 1.0
        for k in OUTCOMES:
            assert pytest.approx(out[k], abs=1e-9) == p[k]

    def test_renormalizes_unnormalized(self):
        p = {"widen": 2.0, "stable": 1.0, "narrow": 1.0}
        out = validate_probs(p)
        assert pytest.approx(sum(out.values()), abs=1e-9) == 1.0
        # Largest stays largest
        assert max(out, key=out.get) == "widen"

    def test_clamps_negative(self):
        p = {"widen": -0.5, "stable": 0.5, "narrow": 0.5}
        out = validate_probs(p)
        assert out["widen"] == 0.0
        assert pytest.approx(sum(out.values()), abs=1e-9) == 1.0

    def test_all_zero_returns_uniform(self):
        out = validate_probs({"widen": 0, "stable": 0, "narrow": 0})
        for k in OUTCOMES:
            assert pytest.approx(out[k], abs=1e-9) == 1 / 3

    def test_missing_keys_default_to_zero_then_renormalize(self):
        out = validate_probs({"widen": 1.0})
        assert pytest.approx(out["widen"], abs=1e-9) == 1.0
        assert out["stable"] == 0.0
        assert out["narrow"] == 0.0


# ---------------------------------------------------------------------------
# brier_score
# ---------------------------------------------------------------------------

class TestBrierScore:
    def test_perfect_prediction_is_zero(self):
        p = {"widen": 1.0, "stable": 0.0, "narrow": 0.0}
        assert brier_score(p, "widen") == 0.0

    def test_completely_wrong_is_two(self):
        p = {"widen": 1.0, "stable": 0.0, "narrow": 0.0}
        # actual=narrow: (1-0)^2 + (0-0)^2 + (0-1)^2 = 2.0
        assert brier_score(p, "narrow") == pytest.approx(2.0)

    def test_uniform_is_two_thirds(self):
        # The well-known baseline — agent's roadmap depends on beating this
        b = brier_score(uniform(), "widen")
        assert pytest.approx(b, abs=1e-9) == 2 / 3

    def test_in_range(self):
        # Any valid prob distribution + any outcome must produce 0 <= b <= 2
        p = {"widen": 0.5, "stable": 0.3, "narrow": 0.2}
        for o in OUTCOMES:
            b = brier_score(p, o)
            assert 0.0 <= b <= 2.0


# ---------------------------------------------------------------------------
# log_loss
# ---------------------------------------------------------------------------

class TestLogLoss:
    def test_perfect_prediction_is_zero(self):
        p = {"widen": 1.0, "stable": 0.0, "narrow": 0.0}
        ll = log_loss(p, "widen")
        assert pytest.approx(ll, abs=1e-9) == 0.0

    def test_zero_prob_on_actual_returns_none(self):
        # Undefined: -log(0) → +inf. Spec is to return None.
        p = {"widen": 1.0, "stable": 0.0, "narrow": 0.0}
        assert log_loss(p, "narrow") is None

    def test_uniform_is_log_3(self):
        ll = log_loss(uniform(), "stable")
        assert pytest.approx(ll, abs=1e-9) == math.log(3)


# ---------------------------------------------------------------------------
# uniform
# ---------------------------------------------------------------------------

class TestUniform:
    def test_sums_to_one(self):
        u = uniform()
        assert pytest.approx(sum(u.values()), abs=1e-9) == 1.0

    def test_each_third(self):
        u = uniform()
        for k in OUTCOMES:
            assert pytest.approx(u[k], abs=1e-9) == 1 / 3


# ---------------------------------------------------------------------------
# payday_features
# ---------------------------------------------------------------------------

class TestPaydayFeatures:
    def test_on_payday_15th(self):
        # 15th is a payday: days_since=0, days_until counts to end-of-month
        ds, du = payday_features(date(2026, 5, 15))
        assert ds == 0
        # May has 31 days, so next payday = May 31 → 16 days away
        assert du == 16

    def test_on_last_day_of_month(self):
        ds, du = payday_features(date(2026, 5, 31))
        assert ds == 0
        # Next payday is June 15 → 15 days away
        assert du == 15

    def test_mid_cycle(self):
        # May 20: 5 days since May 15, 11 days until May 31
        ds, du = payday_features(date(2026, 5, 20))
        assert ds == 5
        assert du == 11

    def test_first_of_month_uses_previous_eom(self):
        # Apr 30 was previous payday → May 1 means ds=1, du=14 (to May 15)
        ds, du = payday_features(date(2026, 5, 1))
        assert ds == 1
        assert du == 14

    def test_accepts_datetime(self):
        ds, du = payday_features(datetime(2026, 5, 20, 12, 0, 0))
        assert ds == 5
        assert du == 11

    def test_accepts_iso_string(self):
        ds, du = payday_features("2026-05-20T12:00:00")
        assert ds == 5
        assert du == 11

    def test_february_short_month(self):
        # Feb 28 (non-leap) is the last day → payday
        ds, du = payday_features(date(2026, 2, 28))
        assert ds == 0
        # Next is March 15 → 15 days
        assert du == 15
