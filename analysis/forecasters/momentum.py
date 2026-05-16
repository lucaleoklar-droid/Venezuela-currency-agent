"""Momentum (trend-extrapolation) forecaster.

Prior: the spread keeps doing what it has recently been doing. This is the
economic *opposite* of NaiveForecaster, which implicitly assumes mean-reversion
to the base rate. Trend-following vs. mean-reversion fail in opposite market
conditions, so this model's errors are strongly decorrelated from naive's —
exactly the diversity an ensemble's ambiguity term rewards.

Mechanics (no learned analogs, no kernel — deliberately a different model family
from the stat forecasters):

  1. Estimate the recent spread *drift* by ordinary least squares of spread on
     time over a short momentum window (default 72h). slope·24h = mu, the
     projected 24h change.
  2. Estimate the *volatility* of 24h spread moves, sigma, from the empirical
     std of historical 24h deltas (same 6h-decimation / ±2h-match sampling the
     other forecasters use, so it's apples-to-apples).
  3. Treat the 24h change as Normal(mu, sigma) and integrate over the frozen
     ±1pp band to get P(narrow) / P(stable) / P(widen).

A small epsilon of uniform mass is mixed in so no class is ever exactly 0
(keeps log loss finite, mirrors the Laplace smoothing elsewhere). Falls back to
uniform when there isn't enough history to estimate either mu or sigma.

Uses only `history` (all rows <= now) — no future leakage.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta

from . import OUTCOMES, Probs, STABILITY_BAND_PP, compute_outcome, uniform, validate_probs

logger = logging.getLogger(__name__)

_MATCH_WINDOW = timedelta(hours=2)
_HORIZON = timedelta(hours=24)
_SAMPLE_GAP = timedelta(hours=6)


def _parse_ts(s):
    if isinstance(s, datetime):
        return s
    return datetime.fromisoformat(s)


def _phi(z: float) -> float:
    """Standard-normal CDF via erf."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


class MomentumForecaster:
    name = "momentum"

    def __init__(
        self,
        lookback_days: int = 30,
        momentum_window_hours: float = 72.0,
        min_slope_points: int = 6,
        min_delta_points: int = 5,
        sigma_floor_pp: float = 0.5,
        epsilon: float = 0.02,
    ):
        self.lookback_days = lookback_days
        self.momentum_window_hours = momentum_window_hours
        self.min_slope_points = min_slope_points
        self.min_delta_points = min_delta_points
        self.sigma_floor_pp = sigma_floor_pp
        self.epsilon = epsilon

    def forecast(self, history: list[dict]) -> Probs:
        usable = []
        for r in history:
            sp = r.get("spread_pct")
            ts = r.get("timestamp")
            if sp is None or ts is None:
                continue
            try:
                t = _parse_ts(ts)
            except (ValueError, TypeError):
                continue
            usable.append((t, float(sp)))

        if len(usable) < self.min_slope_points:
            return uniform()

        now = usable[-1][0]
        cutoff = now - timedelta(days=self.lookback_days)
        window = [(t, s) for (t, s) in usable if t >= cutoff]
        if len(window) < self.min_slope_points:
            return uniform()

        # --- 1. recent drift: OLS slope of spread on hours, momentum window ---
        mom_cutoff = now - timedelta(hours=self.momentum_window_hours)
        recent = [(t, s) for (t, s) in window if t >= mom_cutoff]
        if len(recent) < self.min_slope_points:
            return uniform()

        t0 = recent[0][0]
        xs = [(t - t0).total_seconds() / 3600.0 for (t, _) in recent]
        ys = [s for (_, s) in recent]
        n = len(xs)
        mean_x = sum(xs) / n
        mean_y = sum(ys) / n
        var_x = sum((x - mean_x) ** 2 for x in xs)
        if var_x == 0:
            return uniform()
        cov_xy = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(n))
        slope_per_hour = cov_xy / var_x
        mu = slope_per_hour * _HORIZON.total_seconds() / 3600.0

        # --- 2. volatility: empirical std of historical 24h spread deltas ---
        deltas: list[float] = []
        last_sampled_t = None
        for i, (t, sp) in enumerate(window):
            if last_sampled_t is not None and (t - last_sampled_t) < _SAMPLE_GAP:
                continue
            target = t + _HORIZON
            best = None
            best_dt = None
            for (t2, sp2) in window[i + 1:]:
                dt = abs(t2 - target)
                if dt > _MATCH_WINDOW:
                    if t2 > target + _MATCH_WINDOW:
                        break
                    continue
                if best_dt is None or dt < best_dt:
                    best = sp2
                    best_dt = dt
            if best is None:
                continue
            deltas.append(best - sp)
            last_sampled_t = t

        if len(deltas) < self.min_delta_points:
            return uniform()

        d_mean = sum(deltas) / len(deltas)
        d_var = sum((d - d_mean) ** 2 for d in deltas) / len(deltas)
        sigma = max(math.sqrt(d_var), self.sigma_floor_pp)

        # --- 3. integrate N(mu, sigma) over the frozen ±band ---
        band = STABILITY_BAND_PP
        p_narrow = _phi((-band - mu) / sigma)
        p_widen = 1.0 - _phi((band - mu) / sigma)
        p_stable = 1.0 - p_narrow - p_widen
        if p_stable < 0.0:  # numerical guard
            p_stable = 0.0

        e = self.epsilon
        probs: Probs = {
            "widen": (1 - e) * p_widen + e / 3,
            "stable": (1 - e) * p_stable + e / 3,
            "narrow": (1 - e) * p_narrow + e / 3,
        }
        return validate_probs(probs)
