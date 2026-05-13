"""Statistical forecaster: kernel-weighted nearest-neighbor on standardized features.

Stage 2 forecaster. Learns from past 24h outcomes using three features:
  1. spread_now            : current spread (pp)
  2. spread_change_24h_pp  : Δspread over previous 24h (pp)
  3. parallel_change_24h_pct: parallel rate % change over previous 24h

For the query "now" point, we compute the same features, then weight each
training example by a Gaussian kernel on standardized feature distance and
vote with Laplace smoothing. Falls back to uniform when features are
undefined or training set is too small. Must beat naive on Brier in backtest
to justify its existence.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta

from . import OUTCOMES, Probs, compute_outcome, uniform, validate_probs

logger = logging.getLogger(__name__)

_MATCH_WINDOW = timedelta(hours=2)
_HORIZON = timedelta(hours=24)
_SAMPLE_GAP = timedelta(hours=6)


def _parse_ts(s):
    if isinstance(s, datetime):
        return s
    return datetime.fromisoformat(s)


def _find_near(readings, target_t, start_idx=0):
    """Find the reading closest to target_t within ±_MATCH_WINDOW.
    `readings` is a list of (t, row) ordered ascending. Returns row or None."""
    best = None
    best_dt = None
    for j in range(start_idx, len(readings)):
        t2, row2 = readings[j]
        dt = abs(t2 - target_t)
        if dt > _MATCH_WINDOW:
            if t2 > target_t + _MATCH_WINDOW:
                break
            continue
        if best_dt is None or dt < best_dt:
            best = row2
            best_dt = dt
    return best


def _features_at(readings, idx):
    """Compute (spread_now, spread_change_24h_pp, parallel_change_24h_pct)
    for readings[idx]. Returns None if any feature is undefined."""
    t, row = readings[idx]
    sp_now = row["spread_pct"]
    par_now = row["parallel_rate"]
    if sp_now is None or par_now is None:
        return None

    target = t - _HORIZON
    # Search backward through earlier readings for match within ±2h.
    best = None
    best_dt = None
    for j in range(idx - 1, -1, -1):
        t2, row2 = readings[j]
        dt = abs(t2 - target)
        if dt > _MATCH_WINDOW:
            if t2 < target - _MATCH_WINDOW:
                break
            continue
        if best_dt is None or dt < best_dt:
            best = row2
            best_dt = dt
    if best is None:
        return None
    sp_prev = best.get("spread_pct")
    par_prev = best.get("parallel_rate")
    if sp_prev is None or par_prev is None or par_prev == 0:
        return None

    return (
        float(sp_now),
        float(sp_now) - float(sp_prev),
        (float(par_now) / float(par_prev) - 1.0) * 100.0,
    )


class StatForecaster:
    name = "stat"

    def __init__(
        self,
        lookback_days: int = 30,
        kernel_sigma: float = 1.0,
        smoothing: float = 1.0,
        min_examples: int = 5,
    ):
        self.lookback_days = lookback_days
        self.kernel_sigma = kernel_sigma
        self.smoothing = smoothing
        self.min_examples = min_examples

    def forecast(self, history: list[dict]) -> Probs:
        # Parse + filter readings
        readings = []
        for r in history:
            ts = r.get("timestamp")
            if ts is None:
                continue
            try:
                t = _parse_ts(ts)
            except (ValueError, TypeError):
                continue
            readings.append((t, r))

        if not readings:
            return uniform()

        now = readings[-1][0]
        cutoff = now - timedelta(days=self.lookback_days)
        # Keep readings within lookback (plus we still need older ones for the
        # -24h lookup of training examples near the cutoff edge — but to stay
        # simple, filter to lookback window; features that need t-24h will
        # simply be undefined for very-early examples).
        window = [(t, row) for (t, row) in readings if t >= cutoff]
        if len(window) < 2:
            return uniform()

        # Query features (for "now")
        query = _features_at(window, len(window) - 1)
        if query is None:
            return uniform()

        # Extract training examples with 6h decimation. Critically: t+24h must
        # be ≤ now (history[-1].timestamp), so the outcome is not future-leaked.
        examples = []  # list of (features_tuple, outcome)
        last_sampled_t = None

        for i, (t, row) in enumerate(window):
            # The query point itself (last reading) must not be a training example.
            if i == len(window) - 1:
                continue
            # Outcome target must be within history we have
            if t + _HORIZON > now + _MATCH_WINDOW:
                break  # ordered ascending — no further candidates can satisfy

            if last_sampled_t is not None and (t - last_sampled_t) < _SAMPLE_GAP:
                continue

            feats = _features_at(window, i)
            if feats is None:
                continue

            # Find outcome reading near t + 24h
            target = t + _HORIZON
            outcome_row = _find_near(window, target, start_idx=i + 1)
            if outcome_row is None:
                continue
            sp_after = outcome_row.get("spread_pct")
            if sp_after is None:
                continue
            outcome = compute_outcome(feats[0], float(sp_after))
            examples.append((feats, outcome))
            last_sampled_t = t

        if len(examples) < self.min_examples:
            return uniform()

        # Standardize features across training set
        n_feat = 3
        means = [0.0] * n_feat
        for feats, _ in examples:
            for j in range(n_feat):
                means[j] += feats[j]
        for j in range(n_feat):
            means[j] /= len(examples)

        stds = [0.0] * n_feat
        for feats, _ in examples:
            for j in range(n_feat):
                stds[j] += (feats[j] - means[j]) ** 2
        for j in range(n_feat):
            stds[j] = math.sqrt(stds[j] / len(examples))
            if stds[j] == 0:
                stds[j] = 1.0

        def standardize(f):
            return tuple((f[j] - means[j]) / stds[j] for j in range(n_feat))

        q_std = standardize(query)

        # Gaussian kernel weights + weighted vote with Laplace smoothing
        scores = {k: 0.0 for k in OUTCOMES}
        two_sigma_sq = 2.0 * self.kernel_sigma * self.kernel_sigma

        for feats, outcome in examples:
            x = standardize(feats)
            d2 = sum((x[j] - q_std[j]) ** 2 for j in range(n_feat))
            w = math.exp(-d2 / two_sigma_sq)
            scores[outcome] += w

        total = sum(scores.values())
        a = self.smoothing
        denom = total + 3 * a
        probs: Probs = {k: (scores[k] + a) / denom for k in OUTCOMES}  # type: ignore[assignment]
        return validate_probs(probs)
