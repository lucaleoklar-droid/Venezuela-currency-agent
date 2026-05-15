"""Forecaster math: outcome bucketing, prob validation, Brier, log_loss,
payday features. All pure functions — no DB, no I/O."""
import math
import pytest
from datetime import date, datetime

from analysis.forecasters import (
    OUTCOMES, STABILITY_BAND_PP,
    brier_score, compute_outcome, log_loss, uniform, validate_probs,
)
from analysis.forecasters.payday import payday_features


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
