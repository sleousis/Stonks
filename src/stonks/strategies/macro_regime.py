"""MacroRegimeFilter — gate any strategy on a point-in-time macro regime.

Wraps an inner :class:`Strategy` and reads one series from
``macro_indicators`` (``country_iso`` x ``indicator``). The regime signal is
the latest observation (``transform="level"``) or its change over
``change_periods`` observations (``transform="change"``), using only
observations already published on ``as_of``: macro data describes a date
but is released after that period is over, so an observation counts from
the end of its period plus ``publication_lag_days`` (``observation_stamp``
says whether ``observation_date`` marks the period's start, as EODHD's
annual series do, or its end).

Risk off when the signal is above (or below) ``threshold``. In risk off
``estimate_return`` returns ``None`` for every ticker and ``decide`` exits:
every position, longs sold and shorts covered (``risk_off_exit="all"``),
or only what the inner strategy's own no-picks exit logic closes
(``"inner"``). In risk on the
wrapper is transparent.

When the regime can't be judged (no data, too little history, or the
latest observation published more than ``max_staleness_days`` ago) ``when_unknown``
decides.

Persistence and the inner-strategy plumbing come from
:class:`~stonks.strategies._wrapping.InnerStrategyWrapper`: the params carry
the inner strategy's class path and fully-resolved params, and the inner
strategy saves itself into an ``inner/`` sub-directory so fitted state
survives.

Known limit: the lake keeps the latest vintage of each observation, so a
later revision of an old data point is seen as if it had been published
originally.
"""

from __future__ import annotations

import weakref
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from stonks.core.params import ParameterSpec
from stonks.core.types import Features, Order, Portfolio
from stonks.strategies._common import as_datetime, close_all, closes_only
from stonks.strategies._wrapping import InnerStrategyWrapper, inner_param_specs
from stonks.strategies._wrapping import import_strategy_class as _import_strategy_class

__all__ = ["MacroRegimeFilter", "_import_strategy_class", "load_macro_observations", "macro_signal"]


@dataclass
class _LakeState:
    observations: list[tuple[date, date, float]] | None = None
    regimes: dict[date, tuple[bool, float | None]] = field(default_factory=dict)


class MacroRegimeFilter(InnerStrategyWrapper):
    id = "macro_regime_filter"
    id_suffix = "macro"
    hypothesis = (
        "Risk assets do badly when the economy turns down, and macro "
        "series such as unemployment show the turn. Standing aside then "
        "cuts drawdowns. Adds no alpha of its own and macro data is slow."
    )

    @classmethod
    def parameter_spec(cls):
        return [
            *inner_param_specs(),
            ParameterSpec(
                name="country_iso",
                kind="categorical",
                default="USA",
                bounds=None,
                tunable=False,
                description="ISO 3166-1 alpha-3 country of the macro series.",
            ),
            ParameterSpec(
                name="indicator",
                kind="categorical",
                default="unemployment_total_percent",
                bounds=None,
                tunable=False,
                description="Canonical macro indicator key.",
            ),
            ParameterSpec(
                name="transform",
                kind="categorical",
                default="change",
                bounds=["level", "change"],
                tunable=False,
                description="Signal: the latest value, or its change over change_periods.",
            ),
            ParameterSpec(
                name="change_periods",
                kind="int",
                default=1,
                bounds=(1, 24),
                description="Observations back for the 'change' transform.",
            ),
            ParameterSpec(
                name="threshold",
                kind="float",
                default=0.5,
                bounds=(-1e6, 1e6),
                description="Risk off when the signal crosses this level.",
            ),
            ParameterSpec(
                name="risk_off_when",
                kind="categorical",
                default="above",
                bounds=["above", "below"],
                tunable=False,
                description="Risk off when the signal is above / below threshold.",
            ),
            ParameterSpec(
                name="publication_lag_days",
                kind="int",
                default=180,
                bounds=(0, 730),
                tunable=False,
                description="Days after its period ends an observation is public. "
                "Annual series (World Bank style) appear months into the next year.",
            ),
            ParameterSpec(
                name="observation_stamp",
                kind="categorical",
                default="period_start",
                bounds=["period_start", "period_end"],
                tunable=False,
                description="Which end of its period observation_date marks. EODHD "
                "stamps full-year 2023 as 2023-01-01 ('period_start').",
            ),
            ParameterSpec(
                name="max_staleness_days",
                kind="int",
                default=730,
                bounds=(1, 3650),
                tunable=False,
                description="Latest observation published longer ago than this means regime unknown.",
            ),
            ParameterSpec(
                name="when_unknown",
                kind="categorical",
                default="risk_on",
                bounds=["risk_on", "risk_off"],
                tunable=False,
                description="Regime assumed when it can't be judged from the data.",
            ),
            ParameterSpec(
                name="risk_off_exit",
                kind="categorical",
                default="all",
                bounds=["all", "inner"],
                tunable=False,
                description="On risk off sell every long ('all') or only the inner "
                "strategy's own no-picks exits ('inner').",
            ),
        ]

    def __init__(self, params: Any) -> None:
        super().__init__(params)
        # Per lake (weakly held, so the lab's permuted / perturbed copies
        # never share state): the macro series and the regime per day. The
        # last lake seen lets ``decide`` (which gets no lake) judge a day.
        self._lakes: weakref.WeakKeyDictionary[Any, _LakeState] = weakref.WeakKeyDictionary()

    # ---- regime -------------------------------------------------------------

    def is_risk_off(self, as_of: Any, lake: Any) -> bool:
        return self._regime(as_of, lake)[0]

    def _regime(self, as_of: Any, lake: Any) -> tuple[bool, float | None]:
        day = as_datetime(as_of).date()
        if lake is None:
            return self._classify(None)
        state = self._state(lake)
        cached = state.regimes.get(day)
        if cached is None:
            if state.observations is None:
                state.observations = self._load_observations(lake)
            cached = self._classify(self._signal(day, state.observations))
            state.regimes[day] = cached
        return cached

    def _classify(self, signal: float | None) -> tuple[bool, float | None]:
        if signal is None:
            return self.params["when_unknown"] == "risk_off", None
        threshold = float(self.params["threshold"])
        if self.params["risk_off_when"] == "above":
            return signal > threshold, signal
        return signal < threshold, signal

    def _signal(self, day: date, observations: list[tuple[date, date, float]]) -> float | None:
        return macro_signal(
            day,
            observations,
            transform=self.params["transform"],
            change_periods=int(self.params["change_periods"]),
            max_staleness_days=int(self.params["max_staleness_days"]),
        )

    def _load_observations(self, lake: Any) -> list[tuple[date, date, float]]:
        return load_macro_observations(
            lake,
            self.params["country_iso"],
            self.params["indicator"],
            publication_lag_days=int(self.params["publication_lag_days"]),
            observation_stamp=self.params["observation_stamp"],
        )

    def _state(self, lake: Any) -> _LakeState:
        """This lake's cached state; remembers it as the last lake seen. A
        lake that can't be weakly referenced gets fresh (uncached) state."""
        try:
            state = self._lakes.get(lake)
            if state is None:
                state = _LakeState()
                self._lakes[lake] = state
            self._last_lake = weakref.ref(lake)
        except TypeError:
            self._last_lake = None
            return _LakeState()
        return state

    # ---- Strategy Protocol ---------------------------------------------------

    def extract_features(self, ticker: str, as_of: Any, lake: Any) -> Features:
        inner = self._inner.extract_features(ticker, as_of, lake)
        values = dict(inner.values)
        if lake is not None:
            risk_off, signal = self._regime(as_of, lake)
            values["macro_risk_off"] = 1.0 if risk_off else 0.0
            if signal is not None:
                values["macro_signal"] = signal
        return Features(values=values)

    def estimate_return(self, ticker: str, as_of: Any, lake: Any) -> float | None:
        if lake is not None and self.is_risk_off(as_of, lake):
            return None
        return self._inner.estimate_return(ticker, as_of, lake)

    def decide(
        self,
        my_picks: Sequence[tuple[float, str]],
        portfolio: Portfolio,
        prices: Mapping[str, float],
        as_of: Any,
    ) -> list[Order]:
        lake = self._last_lake() if self._last_lake is not None else None
        # No lake seen yet: the regime can't be judged here, stay transparent.
        if lake is None or not self._regime(as_of, lake)[0]:
            return self._inner.decide(my_picks, portfolio, prices, as_of)
        if self.params["risk_off_exit"] == "inner":
            return closes_only(self._inner.decide([], portfolio, prices, as_of), portfolio)
        return close_all(self.id, portfolio, as_of)


# ---- point-in-time macro loaders (shared with the regime conditions) --------------


def load_macro_observations(
    lake: Any,
    country_iso: str,
    indicator: str,
    *,
    publication_lag_days: int,
    observation_stamp: str,
) -> list[tuple[date, date, float]]:
    """(observation_date, available_date, value) oldest first, NULLs dropped."""
    df = lake.get_macro_series(
        country_iso,
        indicator,
        publication_lag_days=publication_lag_days,
        stamped_at=observation_stamp,
    )
    return [
        (obs, avail, float(v))
        for obs, avail, v in zip(
            df["observation_date"], df["available_date"], df["value"], strict=True
        )
        if v is not None and v == v  # drop NULL / NaN
    ]


def macro_signal(
    day: date,
    observations: list[tuple[date, date, float]],
    *,
    transform: str,
    change_periods: int,
    max_staleness_days: int,
) -> float | None:
    """The latest level (or its change over ``change_periods``
    observations) among observations already published on ``day``;
    ``None`` when there are too few or the latest is stale."""
    visible = [(avail, v) for _obs, avail, v in observations if avail <= day]
    if not visible:
        return None
    # Staleness counts from publication, not from the (period-start)
    # stamp: an annual value stamped Jan 1 is only public ~18 months on.
    latest_avail, latest = visible[-1]
    if (day - latest_avail).days > max_staleness_days:
        return None
    if transform == "level":
        return latest
    if len(visible) <= change_periods:
        return None
    return latest - visible[-1 - change_periods][1]
