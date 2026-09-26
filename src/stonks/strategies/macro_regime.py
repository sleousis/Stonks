"""MacroRegimeFilter — gate any strategy on a point-in-time macro regime.

Wraps an inner :class:`Strategy` and reads one series from
``macro_indicators`` (``country_iso`` x ``indicator``). The regime signal is
the latest observation (``transform="level"``) or its change over
``change_periods`` observations (``transform="change"``), using only
observations already published on ``as_of``: macro data describes a date
but is released later, so an observation counts from
``observation_date + publication_lag_days``.

Risk off when the signal is above (or below) ``threshold``. In risk off
``estimate_return`` returns ``None`` for every ticker and ``decide`` exits:
every long position (``risk_off_exit="all"``) or only what the inner
strategy's own no-picks exit logic sells (``"inner"``). In risk on the
wrapper is transparent.

When the regime can't be judged (no data, too little history, or the
latest observation older than ``max_staleness_days``) ``when_unknown``
decides.

Persistence: the wrapper's params carry the inner strategy's class path
and fully-resolved params, so the registry round-trips it through
``MacroRegimeFilter.load``; the inner strategy also saves itself into an
``inner/`` sub-directory so fitted state survives.

Known limit: the lake keeps the latest vintage of each observation, so a
later revision of an old data point is seen as if it had been published
originally.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import weakref
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any, get_args

from stonks.core.params import ParameterSpec
from stonks.core.protocols import Strategy
from stonks.core.types import AssetClass, Features, Order, Portfolio
from stonks.strategies._common import as_datetime, iso
from stonks.strategies.base import BaseStrategy

_INNER_DIR = "inner"


class MacroRegimeFilter(BaseStrategy):
    id = "macro_regime_filter"
    # Instances mirror their inner strategy; the class default admits all.
    applicable_asset_classes: tuple[AssetClass, ...] = get_args(AssetClass)

    @classmethod
    def parameter_spec(cls):
        return [
            ParameterSpec(
                name="inner_class_path",
                kind="categorical",
                default="stonks.strategies.examples.momentum:Momentum",
                bounds=None,
                tunable=False,
                description="'module:Class' of the wrapped strategy.",
            ),
            ParameterSpec(
                name="inner_params",
                kind="categorical",
                default={},
                bounds=None,
                tunable=False,
                description="Params of the wrapped strategy (a mapping).",
            ),
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
                default=90,
                bounds=(0, 730),
                tunable=False,
                description="Days after observation_date an observation is public.",
            ),
            ParameterSpec(
                name="max_staleness_days",
                kind="int",
                default=730,
                bounds=(1, 3650),
                tunable=False,
                description="Latest observation older than this means regime unknown.",
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
        inner_params = self.params["inner_params"]
        if not isinstance(inner_params, Mapping):
            raise ValueError(f"inner_params must be a mapping, got {type(inner_params).__name__}")
        inner_cls = _import_strategy_class(self.params["inner_class_path"])
        self._inner: Strategy = inner_cls(dict(inner_params))
        self._adopt_inner()
        # series cache per lake; regime per calendar day; last lake seen so
        # ``decide`` (which gets no lake) can judge a day on its own.
        self._series: weakref.WeakKeyDictionary[Any, list[tuple[date, date, float]]]
        self._series = weakref.WeakKeyDictionary()
        self._regimes: dict[date, tuple[bool, float | None]] = {}
        self._last_lake: weakref.ref | None = None

    def _adopt_inner(self) -> None:
        inner = self._inner
        self.params["inner_params"] = dict(getattr(inner, "params", self.params["inner_params"]))
        self.id = f"{getattr(inner, 'id', 'strategy')}_macro"
        self.applicable_asset_classes = tuple(
            getattr(inner, "applicable_asset_classes", ("equity",))
        )

    @property
    def inner(self) -> Strategy:
        return self._inner

    # ---- regime -------------------------------------------------------------

    def is_risk_off(self, as_of: Any, lake: Any) -> bool:
        return self._regime(as_of, lake)[0]

    def _regime(self, as_of: Any, lake: Any) -> tuple[bool, float | None]:
        day = as_datetime(as_of).date()
        if lake is not None:
            self._remember(lake)
        cached = self._regimes.get(day)
        if cached is not None:
            return cached
        signal = self._signal(day, lake) if lake is not None else None
        if signal is None:
            risk_off = self.params["when_unknown"] == "risk_off"
        elif self.params["risk_off_when"] == "above":
            risk_off = signal > float(self.params["threshold"])
        else:
            risk_off = signal < float(self.params["threshold"])
        result = (risk_off, signal)
        if lake is not None:
            self._regimes[day] = result
        return result

    def _signal(self, day: date, lake: Any) -> float | None:
        visible = [(obs, v) for obs, avail, v in self._observations(lake) if avail <= day]
        if not visible:
            return None
        latest_obs, latest = visible[-1]
        if (day - latest_obs).days > int(self.params["max_staleness_days"]):
            return None
        if self.params["transform"] == "level":
            return latest
        n = int(self.params["change_periods"])
        if len(visible) <= n:
            return None
        return latest - visible[-1 - n][1]

    def _observations(self, lake: Any) -> list[tuple[date, date, float]]:
        """(observation_date, available_date, value) oldest first, NULLs dropped."""
        try:
            cached = self._series.get(lake)
        except TypeError:
            cached = None
        if cached is not None:
            return cached
        df = lake.get_macro_series(
            self.params["country_iso"],
            self.params["indicator"],
            publication_lag_days=int(self.params["publication_lag_days"]),
        )
        rows = [
            (obs, avail, float(v))
            for obs, avail, v in zip(
                df["observation_date"], df["available_date"], df["value"], strict=True
            )
            if v is not None and v == v  # drop NULL / NaN
        ]
        with contextlib.suppress(TypeError):  # lake can't be weakly referenced
            self._series[lake] = rows
        return rows

    def _remember(self, lake: Any) -> None:
        try:
            self._last_lake = weakref.ref(lake)
        except TypeError:
            self._last_lake = None

    # ---- Strategy Protocol ---------------------------------------------------

    def fit(self, dataset: Any) -> None:
        self._inner.fit(dataset)

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
        day = as_datetime(as_of).date()
        known = day in self._regimes or lake is not None
        if not known or not self._regime(as_of, lake)[0]:
            return self._inner.decide(my_picks, portfolio, prices, as_of)
        if self.params["risk_off_exit"] == "inner":
            return [o for o in self._inner.decide([], portfolio, prices, as_of) if o.side == "sell"]
        return [
            Order(
                client_id=f"{self.id}:sell:{ticker}:{iso(as_of)}",
                ticker=ticker,
                side="sell",
                quantity=qty,
                order_type="market",
                strategy_id=self.id,
            )
            for ticker, qty in portfolio.positions.items()
            if qty > 0
        ]

    # ---- persistence -----------------------------------------------------------

    def save(self, path: Path) -> None:
        super().save(path)
        self._inner.save(Path(path) / _INNER_DIR)

    @classmethod
    def load(cls, path: Path) -> MacroRegimeFilter:
        path = Path(path)
        params = json.loads((path / "params.json").read_text())
        instance = cls(params)
        inner_dir = path / _INNER_DIR
        if inner_dir.is_dir():
            inner_cls = _import_strategy_class(params["inner_class_path"])
            instance._inner = inner_cls.load(inner_dir)
            instance._adopt_inner()
        return instance


def _import_strategy_class(class_path: Any) -> type:
    if not isinstance(class_path, str) or ":" not in class_path:
        raise ValueError(f"inner_class_path must be 'module:Class', got {class_path!r}")
    module_name, cls_name = class_path.split(":", 1)
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ValueError(f"inner_class_path {class_path!r}: cannot import module") from exc
    cls = getattr(module, cls_name, None)
    required = ("parameter_spec", "estimate_return", "decide", "save", "load")
    if not isinstance(cls, type) or not all(hasattr(cls, a) for a in required):
        raise ValueError(f"inner_class_path {class_path!r} is not a Strategy class")
    return cls
