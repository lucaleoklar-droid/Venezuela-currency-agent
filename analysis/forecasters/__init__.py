"""Forecaster contract + scoring math.

Every forecaster is a callable conforming to the Forecaster protocol:
  forecast(history: list[dict]) -> Probs

where `history` is a list of rate readings (dicts with 'timestamp', 'bcv_rate',
'parallel_rate', 'spread_pct'), strictly ordered ascending by timestamp, with
NO future leakage — `history[-1]` is "now" from the forecaster's perspective.

The forecaster returns Probs: {"widen": p1, "stable": p2, "narrow": p3} summing
to 1.0. These are 24h forecasts of how the spread will move:

  widen  : spread at t+24h is >+1pp higher than at t
  stable : within ±1pp
  narrow : >-1pp lower

This bucketing is *the* outcome definition. Frozen. Don't change without
invalidating prior scores.
"""
from typing import Protocol, TypedDict
import math

OUTCOMES = ("widen", "stable", "narrow")
STABILITY_BAND_PP = 1.0  # ±1 percentage-point dead zone
HORIZON_HOURS = 24


class Probs(TypedDict):
    widen: float
    stable: float
    narrow: float


class Forecaster(Protocol):
    name: str
    def forecast(self, history: list[dict]) -> Probs: ...


def compute_outcome(spread_before: float, spread_after: float) -> str:
    """Frozen outcome definition: bucket the 24h spread change."""
    delta = spread_after - spread_before
    if delta > STABILITY_BAND_PP:
        return "widen"
    if delta < -STABILITY_BAND_PP:
        return "narrow"
    return "stable"


def validate_probs(probs: Probs) -> Probs:
    """Clamp + renormalize. Forecasters shouldn't emit invalid probs but be defensive."""
    p = {k: max(0.0, float(probs.get(k, 0.0))) for k in OUTCOMES}
    s = sum(p.values())
    if s == 0:
        # Uniform fallback
        return {"widen": 1/3, "stable": 1/3, "narrow": 1/3}
    return {k: v / s for k, v in p.items()}


def brier_score(probs: Probs, actual: str) -> float:
    """Multiclass Brier: sum((p_i - y_i)^2) over the 3 outcomes.
    Range: [0, 2]. Lower is better. Perfect = 0. Always-uniform = 0.667."""
    p = validate_probs(probs)
    return sum((p[k] - (1.0 if k == actual else 0.0)) ** 2 for k in OUTCOMES)


def log_loss(probs: Probs, actual: str) -> float | None:
    """Negative log-likelihood of the actual outcome. Lower is better.
    Returns None if the model assigned 0 prob to actual (undefined)."""
    p = validate_probs(probs)
    pa = p[actual]
    if pa <= 0:
        return None
    return -math.log(pa)


def uniform() -> Probs:
    return {"widen": 1/3, "stable": 1/3, "narrow": 1/3}
