"""Equal forecast weights: the baseline, and what Carver suggests when
there is too little data to estimate anything."""

from __future__ import annotations

from stonks.features.forecast_weights.base import (
    ForecastWeightEstimator,
    WeightInput,
    register_weight_estimator,
)


@register_weight_estimator("equal")
class EqualWeights(ForecastWeightEstimator):
    """``1 / n`` for each of the ``n`` rules."""

    def estimate(self, inp: WeightInput) -> dict[str, float]:
        rules = inp.rules
        return {r: 1.0 / len(rules) for r in rules} if rules else {}
