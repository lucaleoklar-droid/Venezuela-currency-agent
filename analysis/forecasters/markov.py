"""Markov regime-transition forecaster.

Prior: 24h spread-move regimes are *sticky / transition-structured* — the
probability of tomorrow's move depends on the move that just happened. This is
a different mechanism from both the stat family (kernel analogs on features) and
momentum (linear trend), so its errors are decorrelated from theirs.

Mechanics:

  1. Sample the history with 6h decimation; for each sampled point compute the
     realized 24h outcome via the frozen ±1pp band (same sampling the other
     forecasters use — no leakage: only pairs with t+24h <= now are used).
  2. Build a first-order transition count matrix over consecutive sampled
     outcomes: counts[prev][next].
  3. The "current state" is the most recently *completed* 24h outcome —
     compute_outcome(spread at ~now-24h, spread at now). Both endpoints are in
     the past, so no leakage.
  4. Forecast = Laplace-smoothed transition row for the current state.

Fallbacks, in order: if the current state's transition row is too thin, back
off to the Laplace-smoothed *marginal* outcome distribution (still informative,
like the naive base rate); if there are no usable outcomes at all, uniform.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from . import OUTCOMES, Probs, compute_outcome, uniform

logger = logging.getLogger(__name__)

_MATCH_WINDOW = timedelta(hours=2)
_HORIZON = timedelta(hours=24)
_SAMPLE_GAP = timedelta(hours=6)


def _parse_ts(s):
    if isinstance(s, datetime):
        return s
    return datetime.fromisoformat(s)


def _nearest(window, target, start=0):
    """Closest (t, spread) to `target` within ±_MATCH_WINDOW, or None.
    `window` is ascending (t, spread)."""
    best = None
    best_dt = None
    for j in range(start, len(window)):
        t2, sp2 = window[j]
        dt = abs(t2 - target)
        if dt > _MATCH_WINDOW:
            if t2 > target + _MATCH_WINDOW:
                break
            continue
        if best_dt is None or dt < best_dt:
            best = sp2
            best_dt = dt
    return best


class MarkovForecaster:
    name = "markov"

    def __init__(
        self,
        lookback_days: int = 30,
        smoothing: float = 1.0,
        min_transitions: int = 5,
        min_row_total: int = 3,
    ):
        self.lookback_days = lookback_days
        self.smoothing = smoothing
        self.min_transitions = min_transitions
        self.min_row_total = min_row_total

    def _smoothed(self, counts: dict, total: float) -> Probs:
        a = self.smoothing
        denom = total + 3 * a
        return {k: (counts.get(k, 0.0) + a) / denom for k in OUTCOMES}  # type: ignore[return-value]

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

        if len(usable) < 2:
            return uniform()

        now = usable[-1][0]
        cutoff = now - timedelta(days=self.lookback_days)
        window = [(t, s) for (t, s) in usable if t >= cutoff]
        if len(window) < 2:
            return uniform()

        # --- 1. decimated sequence of realized 24h outcomes ---
        seq: list[str] = []  # time-ordered outcomes
        last_sampled_t = None
        for i, (t, sp) in enumerate(window):
            if last_sampled_t is not None and (t - last_sampled_t) < _SAMPLE_GAP:
                continue
            sp_after = _nearest(window, t + _HORIZON, start=i + 1)
            if sp_after is None:
                continue
            seq.append(compute_outcome(sp, sp_after))
            last_sampled_t = t

        if not seq:
            return uniform()

        marginal = {k: 0.0 for k in OUTCOMES}
        for o in seq:
            marginal[o] += 1.0

        # --- 2. first-order transition counts ---
        trans = {a: {b: 0.0 for b in OUTCOMES} for a in OUTCOMES}
        n_trans = 0
        for a, b in zip(seq, seq[1:]):
            trans[a][b] += 1.0
            n_trans += 1

        if n_trans < self.min_transitions:
            # Not enough structure for a transition model — back off to marginal.
            return self._smoothed(marginal, float(len(seq)))

        # --- 3. current state: most recent completed 24h outcome ---
        sp_now = window[-1][1]
        sp_prev = _nearest(window, now - _HORIZON, start=0)
        if sp_prev is None:
            return self._smoothed(marginal, float(len(seq)))
        current = compute_outcome(sp_prev, sp_now)

        # --- 4. transition row for current state, with marginal back-off ---
        row = trans[current]
        row_total = sum(row.values())
        if row_total < self.min_row_total:
            return self._smoothed(marginal, float(len(seq)))
        return self._smoothed(row, row_total)
