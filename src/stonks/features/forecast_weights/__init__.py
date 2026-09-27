"""Carver-style forecast weights estimated net of costs (roadmap 22.7).

- :mod:`.base`: the :class:`ForecastWeightEstimator` seam and its registry.
- :mod:`.equal`, :mod:`.handcraft`, :mod:`.bootstrap`: the estimators.
- :mod:`.fit`: scaling, capping, costs in Sharpe units, the speed limit,
  the weights and the FDM for one instrument's training rows.
"""

from stonks.features.forecast_weights.base import (
    ForecastWeightEstimator,
    WeightInput,
    get_weight_estimator,
    normalise,
    register_weight_estimator,
    weight_estimator_names,
)
from stonks.features.forecast_weights.fit import (
    DEFAULT_MAX_COST_SR,
    MIN_FIT_OBSERVATIONS,
    ForecastWeightFit,
    RuleFit,
    fit_forecast_weights,
    forecast_turnover,
    rule_net_returns,
)

__all__ = [
    "DEFAULT_MAX_COST_SR",
    "MIN_FIT_OBSERVATIONS",
    "ForecastWeightEstimator",
    "ForecastWeightFit",
    "RuleFit",
    "WeightInput",
    "fit_forecast_weights",
    "forecast_turnover",
    "get_weight_estimator",
    "normalise",
    "register_weight_estimator",
    "rule_net_returns",
    "weight_estimator_names",
]
