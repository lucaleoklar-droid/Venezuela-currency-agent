"""Stage 3 forecaster: stat features + operational signals.

Extends StatForecaster with two exogenous features:
  4. brent_change_7d_pct        : % change in Brent over previous 7 days
  5. news_intervention_count_7d : count of FX-intervention news headlines, last 7d

Same kernel-weighted nearest-neighbor machinery as Stage 2. 5 features instead
of 3. Falls back to uniform when features are undefined or training set is too
small. Must beat Stat on Brier — both in backtest AND in live scoring — before
earning the Stage-4 (Claude synthesizer) promotion.

History rows are expected to have already been enriched (see analysis.forecasters.enrich).
Each row must contain keys: timestamp, bcv_rate, parallel_rate, spread_pct,
brent_usd_per_bbl, news_count_7d.
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
    """Compute the 5-feature vector at readings[idx].

    Returns tuple or None if any feature is undefined. Stage-2 features come
    from the previous-24h rate snapshot; Brent feature comes from previous-7d
    Brent observation; news feature is read directly off the enriched row.
    """
    t, row = readings[idx]
    sp_now = row.get("spread_pct")
    par_now = row.get("parallel_rate")
    if sp_now is None or par_now is None:
        return None

    # ---- Stage-2 features: prev-24h spread / parallel deltas ----
    target_24h = t - _HORIZON
    prev24 = None
    prev24_dt = None
    for j in range(idx - 1, -1, -1):
        t2, row2 = readings[j]
        dt = abs(t2 - target_24h)
        if dt > _MATCH_WINDOW:
            if t2 < target_24h - _MATCH_WINDOW:
                break
            continue
        if prev24_dt is None or dt < prev24_dt:
            prev24 = row2
            prev24_dt = dt
    if prev24 is None:
        return None
    sp_prev = prev24.get("spread_pct")
    par_prev = prev24.get("parallel_rate")
    if sp_prev is None or par_prev is None or par_prev == 0:
        return None

    spread_change_24h_pp = float(sp_now) - float(sp_prev)
    parallel_change_24h_pct = (float(par_now) / float(par_prev) - 1.0) * 100.0

    # ---- Stage-3 feature 4: brent_change_7d_pct ----
    # Both ends pre-computed by analysis.forecasters.enrich against oil_prices.
    brent_now = row.get("brent_usd_per_bbl")
    brent_prev = row.get("brent_usd_per_bbl_7d_ago")
    if brent_now is None or brent_prev is None or brent_prev == 0:
        return None
    brent_change_7d_pct = (float(brent_now) / float(brent_prev) - 1.0) * 100.0

    # ---- Stage-3 feature 5: news count (already on the row) ----
    news_count = row.get("news_count_7d", 0)
    if news_count is None:
        news_count = 0

    return (
        float(sp_now),
        spread_change_24h_pp,
        parallel_change_24h_pct,
        brent_change_7d_pct,
        float(news_count),
    )


class StatV2Forecaster:
    name = "stat_v2"

    def __init__(
        self,
        lookback_days: int = 30,
        kernel_sigma: float = 1.2,
        smoothing: float = 1.0,
        min_examples: int = 8,
    ):
        # kernel_sigma defaults wider than Stat (1.0 → 1.2): with 5 features
        # rather than 3, nearest-neighbor distances grow, so the kernel needs
        # to be a little more forgiving to avoid one-vote dominance.
        # min_examples 8 (vs Stat's 5) for the same reason — more dims need
        # more support before we trust the vote.
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
        window = [(t, row) for (t, row) in readings if t >= cutoff]
        if len(window) < 2:
            return uniform()

        # Query features (for "now")
        query = _features_at(window, len(window) - 1)
        if query is None:
            return uniform()

        # Extract training examples with 6h decimation. t+24h must be ≤ now.
        examples = []  # list of (features_tuple, outcome)
        last_sampled_t = None

        for i, (t, row) in enumerate(window):
            if i == len(window) - 1:
                continue  # query point, never a training example
            if t + _HORIZON > now + _MATCH_WINDOW:
                break

            if last_sampled_t is not None and (t - last_sampled_t) < _SAMPLE_GAP:
                continue

            feats = _features_at(window, i)
            if feats is None:
                continue

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

        # Standardize across training set
        n_feat = 5
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
