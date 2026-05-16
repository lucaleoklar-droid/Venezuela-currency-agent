"""Ensemble forecaster — linear opinion pool over a diversified roster.

Theory: for a linear pool p̄ = Σ wₘ pₘ, the Brier score decomposes exactly as

    Brier(p̄) = Σ wₘ·Brier(pₘ)  −  Σ wₘ·‖pₘ − p̄‖²
               └ avg member error ┘  └─── diversity ───┘

so the pool always beats the weighted-average member, by an amount equal to how
much the members disagree. The gain therefore comes from members whose *errors
are decorrelated*, which requires different model families — not re-tuned copies
of one. The default roster is deliberately diverse:

  - naive      : mean-reversion to the base rate (uses no situational info)
  - stat_v3    : kernel analogs on 7 features (local, nonparametric)
  - momentum   : linear trend extrapolation (opposite prior to naive)
  - markov     : regime-transition structure (different mechanism entirely)

Equal weights by design: with only ~weeks of scored data, estimated "optimal"
weights are dominated by noise (Var(ŵ) ~ O(M/N)); equal weights are the robust
choice until N is large. Revisit weighting once n ≥ 30.

Self-contained — it instantiates and runs its own members, so it conforms to
the plain Forecaster protocol and works unchanged in the backtest harness. A
member that raises or returns garbage is dropped and the pool renormalizes over
the survivors; if every member fails, uniform.
"""
from __future__ import annotations

import logging

from . import OUTCOMES, Probs, uniform, validate_probs
from .naive import NaiveForecaster
from .stat_v3 import StatV3Forecaster
from .momentum import MomentumForecaster
from .markov import MarkovForecaster

logger = logging.getLogger(__name__)


def _default_members():
    return [
        NaiveForecaster(),
        StatV3Forecaster(),
        MomentumForecaster(),
        MarkovForecaster(),
    ]


class EnsembleForecaster:
    name = "ensemble"

    def __init__(self, members=None, weights=None):
        self.members = members if members is not None else _default_members()
        if weights is None:
            n = len(self.members) or 1
            self.weights = [1.0 / n] * len(self.members)
        else:
            if len(weights) != len(self.members):
                raise ValueError("weights length must match members length")
            s = float(sum(weights))
            if s <= 0:
                raise ValueError("weights must sum to a positive value")
            self.weights = [w / s for w in weights]

    def forecast(self, history: list[dict]) -> Probs:
        pooled = {k: 0.0 for k in OUTCOMES}
        total_w = 0.0

        for member, w in zip(self.members, self.weights):
            try:
                raw = member.forecast(history)
                p = validate_probs(raw)
            except Exception as e:  # a broken member must not sink the pool
                logger.warning(
                    "Ensemble member %s failed (%s) — dropping it this cycle",
                    getattr(member, "name", "?"), e,
                )
                continue
            for k in OUTCOMES:
                pooled[k] += w * p[k]
            total_w += w

        if total_w == 0.0:
            return uniform()

        return validate_probs({k: pooled[k] / total_w for k in OUTCOMES})
