"""Built-in portfolio constructors (BL-08).

- ``single_winner``: today's tick behaviour, kept for back-compat and
  parity: the strategy owning the best raw pick takes the whole book,
  equal-weighted over its picks.
- ``equal_weight_top_n``: the ``n`` best combined scores, equal weight.
- ``inverse_vol``: the ``top_n`` best combined scores, ``w_i ∝ 1/sigma_i``.
- ``vol_target``: Carver's volatility-targeted positions,
  ``w_i = tau * IDM * iw_i * F_i / 10 / sigma_i`` with the combined forecast
  ``F_i = clip(FDM * sum_j s_j f_ij, -20, 20)``.

All of them are long-only by default and scaled so gross never exceeds
``max_gross`` (1.0 in a cash account); weights left over are cash.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Literal

import numpy as np
import pandas as pd
from pydantic import Field

from stonks.portfolio.base import (
    ConstructionInput,
    ConstructorSettings,
    PortfolioConstructor,
    TargetBook,
    combine_signals,
    register_constructor,
)
from stonks.portfolio.signals import FORECAST_CAP, FORECAST_TARGET

MAX_DIVERSIFICATION_MULTIPLIER = 2.5
MIN_OVERLAP_OBSERVATIONS = 20


def diversification_multiplier(
    history: pd.DataFrame | None,
    weights: Mapping[str, float],
    *,
    min_observations: int = MIN_OVERLAP_OBSERVATIONS,
    cap: float = MAX_DIVERSIFICATION_MULTIPLIER,
) -> float:
    """Carver's diversification multiplier ``min(cap, 1/sqrt(w' H w))`` with
    ``H`` the correlation of ``history``'s columns (floored at 0) over rows
    where every weighted column is present. Falls back to 1.0 with a single
    column, a column missing from ``history``, or fewer than
    ``min_observations`` overlapping rows. Used for the FDM (columns are
    strategies' forecasts) and the IDM (columns are instruments' returns)."""
    keys = [k for k, w in weights.items() if w > 0]
    if history is None or len(keys) < 2 or any(k not in history.columns for k in keys):
        return 1.0
    overlap = history[keys].dropna()
    if len(overlap) < min_observations:
        return 1.0
    corr = overlap.corr().to_numpy()
    # a constant column has no correlation; treat it as uncorrelated
    corr = np.nan_to_num(corr, nan=0.0)
    np.fill_diagonal(corr, 1.0)
    corr = np.clip(corr, 0.0, None)
    w = np.array([weights[k] for k in keys], dtype=float)
    w = w / w.sum()
    variance = float(w @ corr @ w)
    if variance <= 0:
        return cap
    return min(cap, 1.0 / math.sqrt(variance))


def _top(scores: Mapping[str, float], n: int | None) -> list[str]:
    """Tickers with a positive score, best first (ties by ticker), at most n."""
    ranked = sorted((t for t, v in scores.items() if v > 0), key=lambda t: (-scores[t], t))
    return ranked if n is None else ranked[:n]


# --- single winner --------------------------------------------------------------


class SingleWinnerSettings(ConstructorSettings):
    threshold: float = 0.0


@register_constructor("single_winner")
class SingleWinner(PortfolioConstructor):
    """Winner-take-all, as ``production/tick.py`` does today: rank every
    ``(score, strategy, ticker)`` above ``threshold`` by raw score (a stable
    sort, so ties keep strategy then ticker order), give the book to the
    strategy owning the top pick, and equal-weight its picks.
    ``meta`` carries ``winner_strategy_id`` and ``picks`` (``[(score,
    ticker)]``, best first) exactly as the tick hands them to ``decide``."""

    signal_method = "raw"
    Settings = SingleWinnerSettings

    def target_weights(self, inp: ConstructionInput) -> TargetBook:
        threshold = self.settings.threshold
        ranked = [
            (float(r), sid, t)
            for sid, scores in inp.signals.items()
            for t, r in scores.items()
            if r is not None and math.isfinite(r) and r > threshold
        ]
        ranked.sort(key=lambda p: p[0], reverse=True)
        if not ranked:
            return self.finalize({}, meta={"winner_strategy_id": None, "picks": []})
        winner = ranked[0][1]
        picks = [(r, t) for r, sid, t in ranked if sid == winner]
        tradable = [t for _, t in picks if inp.tradable(t)]
        weights = {t: self.settings.max_gross / len(tradable) for t in tradable}
        return self.finalize(
            weights,
            attribution={t: {winner: 1.0} for t in tradable},
            meta={"winner_strategy_id": winner, "picks": picks},
        )


# --- equal weight ---------------------------------------------------------------


class EqualWeightSettings(ConstructorSettings):
    n: int = Field(default=10, ge=1)


@register_constructor("equal_weight_top_n")
class EqualWeightTopN(PortfolioConstructor):
    """Equal weight across the ``n`` best positive combined scores.

    Signals are percentile ranks (RS-06): the pipeline passes only scores
    above the threshold, so every input is a buy, and a z-score would clip
    the below-mean half to 0 in long-only mode."""

    signal_method = "rank"
    Settings = EqualWeightSettings

    def target_weights(self, inp: ConstructionInput) -> TargetBook:
        combined, attribution = combine_signals(inp)
        eligible = {t: v for t, v in combined.items() if inp.tradable(t)}
        chosen = _top(eligible, self.settings.n)
        weights = {t: self.settings.max_gross / len(chosen) for t in chosen}
        return self.finalize(weights, attribution)


# --- inverse vol ----------------------------------------------------------------


class InverseVolSettings(ConstructorSettings):
    top_n: int = Field(default=20, ge=1)


@register_constructor("inverse_vol")
class InverseVol(PortfolioConstructor):
    """``w_i ∝ 1/sigma_i`` across the ``top_n`` best positive combined scores
    that have a usable volatility, scaled to ``max_gross``. Signals are
    percentile ranks, as for ``equal_weight_top_n`` (RS-06)."""

    signal_method = "rank"
    Settings = InverseVolSettings

    def target_weights(self, inp: ConstructionInput) -> TargetBook:
        combined, attribution = combine_signals(inp)
        eligible = {t: v for t, v in combined.items() if inp.tradable(t) and inp.vol(t) is not None}
        chosen = _top(eligible, self.settings.top_n)
        inverse = {t: 1.0 / inp.vol(t) for t in chosen}  # type: ignore[operator]
        total = sum(inverse.values())
        weights = {t: self.settings.max_gross * v / total for t, v in inverse.items()}
        return self.finalize(weights, attribution)


# --- vol target -----------------------------------------------------------------


class VolTargetSettings(ConstructorSettings):
    tau: float = Field(default=0.20, ge=0.05, le=0.40)
    idm: Literal["auto"] | float = "auto"
    instrument_weight: Literal["equal", "unit"] = "equal"

    def model_post_init(self, __context: object) -> None:
        if self.idm != "auto" and not 0.0 < float(self.idm) <= MAX_DIVERSIFICATION_MULTIPLIER:
            raise ValueError(f"idm must be 'auto' or in (0, 2.5], got {self.idm}")


@register_constructor("vol_target")
class VolTarget(PortfolioConstructor):
    """Carver's position sizing. Signals must be forecasts (mean |f| = 10).

    - ``F_i = clip(FDM * sum_j s_j f_ij, -20, 20)``; ``FDM`` from
      ``forecast_history`` via :func:`diversification_multiplier` (1.0
      without enough history).
    - ``w_i = tau * IDM * iw_i * F_i / 10 / sigma_i``. With
      ``instrument_weight="equal"`` (default) ``iw_i = 1/N`` over the N
      tradable names with a forecast and a volatility, so the book targets
      ``tau`` when forecasts average 10 (Carver ch. 11). A name whose
      forecast is 0 holds nothing but keeps its slice: instrument weights
      are a fixed allocation over the instrument set, not over today's
      longs; ``"unit"`` sets
      ``iw_i = 1`` (every name sized for ``tau`` on its own).
    - ``idm="auto"`` estimates it from ``returns_history`` with the same
      weights; otherwise the given value.
    - Gross is then capped at ``max_gross``.
    """

    signal_method = "forecast"
    Settings = VolTargetSettings

    def target_weights(self, inp: ConstructionInput) -> TargetBook:
        s = self.settings
        strategy_weights = inp.normalized_strategy_weights()
        fdm = diversification_multiplier(inp.forecast_history, strategy_weights)
        combined, attribution = combine_signals(inp)
        eligible = [t for t in combined if inp.tradable(t) and inp.vol(t) is not None]
        if not eligible:
            return self.finalize({}, meta={"fdm": fdm, "idm": 1.0})
        iw = 1.0 / len(eligible) if s.instrument_weight == "equal" else 1.0
        if s.idm == "auto":
            idm = diversification_multiplier(
                inp.returns_history, dict.fromkeys(eligible, 1.0 / len(eligible))
            )
        else:
            idm = float(s.idm)
        weights: dict[str, float] = {}
        for t in eligible:
            forecast = max(-FORECAST_CAP, min(FORECAST_CAP, fdm * combined[t]))
            weights[t] = s.tau * idm * iw * forecast / FORECAST_TARGET / inp.vol(t)  # type: ignore[operator]
        return self.finalize(weights, attribution, meta={"fdm": fdm, "idm": idm})
