"""Naive base-rate forecaster.

Emits the empirical frequency of past 24h outcomes (widen/stable/narrow)
over a lookback window as its forecast. This is the floor benchmark — any
fancier forecaster must beat it on Brier / log loss to justify its complexity.

Laplace (add-one) smoothing is applied so that no outcome class ever has
probability zero. This ensures log loss remains finite even when a class
hasn't been observed yet in the (short) history.
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


class NaiveForecaster:
    name = "naive"

    def __init__(self, lookback_days: int = 30, smoothing: float = 1.0):
        self.lookback_days = lookback_days
        self.smoothing = smoothing

    def forecast(self, history: list[dict]) -> Probs:
        # Filter usable readings
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

        if not usable:
            return uniform()

        now = usable[-1][0]
        cutoff = now - timedelta(days=self.lookback_days)
        window = [(t, s) for (t, s) in usable if t >= cutoff]

        if not window:
            return uniform()

        # Generate training examples with 6h sampling decimation.
        counts = {k: 0 for k in OUTCOMES}
        n = 0
        last_sampled_t = None

        for i, (t, sp) in enumerate(window):
            if last_sampled_t is not None and (t - last_sampled_t) < _SAMPLE_GAP:
                continue
            # Find closest reading to t + 24h within ±2h
            target = t + _HORIZON
            best = None
            best_dt = None
            for (t2, sp2) in window[i + 1:]:
                dt = abs(t2 - target)
                if dt > _MATCH_WINDOW:
                    if t2 > target + _MATCH_WINDOW:
                        break  # ordered ascending; no further candidates
                    continue
                if best_dt is None or dt < best_dt:
                    best = (t2, sp2)
                    best_dt = dt
            if best is None:
                continue
            outcome = compute_outcome(sp, best[1])
            counts[outcome] += 1
            n += 1
            last_sampled_t = t

        if n == 0:
            return uniform()

        a = self.smoothing
        denom = n + 3 * a
        return {k: (counts[k] + a) / denom for k in OUTCOMES}  # type: ignore[return-value]
