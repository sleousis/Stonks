"""Shared plumbing for strategies that wrap an inner strategy.

Every wrapper (regime, macro regime, feature regime, trailing stop, last
trade filter) builds on :class:`InnerStrategyWrapper`: the wrapper's params
carry ``inner_class_path`` + ``inner_params`` (fully resolved after
construction), the wrapper adopts the inner strategy's
``applicable_asset_classes``, ``label_horizon_bars`` and
``required_history_bars`` (each at least the wrapper's own class value) and
forwards its ``data_tickers``, and ``save`` writes the inner strategy into
an ``inner/`` sub-directory so its fitted state survives a registry
round-trip.
"""

from __future__ import annotations

import importlib
import json
import weakref
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar, get_args

from stonks.core.params import ParameterSpec
from stonks.core.protocols import Strategy
from stonks.core.types import AssetClass, Features
from stonks.strategies.base import BaseStrategy, strategy_data_tickers

INNER_DIR = "inner"
INTERVALS = ["1m", "5m", "15m", "30m", "1h", "4h", "12h", "1d", "1w"]


def import_strategy_class(class_path: Any) -> type:
    """The strategy class at ``'module:Class'``; ``ValueError`` otherwise."""
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


def inner_param_specs() -> list[ParameterSpec]:
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
    ]


class InnerStrategyWrapper(BaseStrategy):
    """Base for wrappers; subclasses set ``id_suffix`` and add behavior."""

    id_suffix: ClassVar[str] = "wrapped"
    #: The wrapper's own logic only handles long trades: it never shorts,
    #: whatever its inner strategy does.
    long_only: ClassVar[bool] = False
    # Instances mirror their inner strategy; the class default admits all.
    applicable_asset_classes: tuple[AssetClass, ...] = get_args(AssetClass)

    def __init__(self, params: Any) -> None:
        super().__init__(params)
        inner_params = self.params["inner_params"]
        if not isinstance(inner_params, Mapping):
            raise ValueError(f"inner_params must be a mapping, got {type(inner_params).__name__}")
        inner_cls = import_strategy_class(self.params["inner_class_path"])
        self._inner: Strategy = inner_cls(dict(inner_params))
        self._adopt_inner()
        self._last_lake: weakref.ref | None = None

    def _adopt_inner(self) -> None:
        inner = self._inner
        self.params["inner_params"] = dict(getattr(inner, "params", self.params["inner_params"]))
        self.id = f"{getattr(inner, 'id', 'strategy')}_{self.id_suffix}"
        self.applicable_asset_classes = tuple(
            getattr(inner, "applicable_asset_classes", ("equity",))
        )
        # BE-14: a wrapper shorts exactly when its inner strategy does,
        # unless its own logic is long-only
        self.supports_short = not type(self).long_only and bool(
            getattr(inner, "supports_short", False)
        )
        # RS-02: the embargo, walk-forward and the preflight read these from
        # the wrapper, so they must cover what the inner strategy needs.
        cls = type(self)
        for attr in ("label_horizon_bars", "required_history_bars"):
            own = int(getattr(cls, attr, 0) or 0)
            setattr(self, attr, max(own, int(getattr(inner, attr, 0) or 0)))

    def data_tickers(self) -> tuple[str, ...]:
        """The inner strategy's data tickers (subclasses add their own)."""
        return strategy_data_tickers(self._inner)

    # ---- forecaster hooks (roadmap 23.11): the inner strategy's model runs --------

    @classmethod
    def forecast_models(cls, params: Mapping[str, Any]) -> tuple[str, ...]:
        """The forecasters the inner strategy of ``params`` may run, so the
        pretraining cutoff rule sees a wrapped model too (P12)."""
        from stonks.features.forecasters.cutoff import strategy_forecast_models

        params = dict(params or {})
        defaults = {s.name: s.default for s in cls.parameter_spec()}
        inner_cls = import_strategy_class(
            params.get("inner_class_path", defaults.get("inner_class_path"))
        )
        inner_params = params.get("inner_params", defaults.get("inner_params")) or {}
        return strategy_forecast_models(inner_cls, dict(inner_params))

    def _inner_hook(self, name: str) -> Any:
        value = getattr(self._inner, name, None)
        if value is None:
            raise AttributeError(name)
        return value

    @property
    def forecaster(self) -> Any:
        """The inner strategy's ``forecaster`` hook (``AttributeError``
        when it has none, so ``getattr(..., None)`` reads as absent)."""
        return self._inner_hook("forecaster")

    @property
    def forecast_horizon(self) -> Any:
        return self._inner_hook("forecast_horizon")

    @property
    def forecast_context_bars(self) -> Any:
        return self._inner_hook("forecast_context_bars")

    @property
    def inner(self) -> Strategy:
        return self._inner

    #: The wrapper fits a model of its own (a latent regime), besides
    #: whatever its inner strategy fits.
    fits_itself: ClassVar[bool] = False

    @property
    def retrainable(self) -> bool:
        """The wrapper retrains when it fits a model of its own or its
        inner strategy does (roadmap 22.6)."""
        return self.fits_itself or bool(getattr(self._inner, "retrainable", False))

    def _remember_lake(self, lake: Any) -> None:
        """``decide`` gets no lake; remember the last one seen (weakly)."""
        try:
            self._last_lake = weakref.ref(lake)
        except TypeError:
            self._last_lake = None

    def _recall_lake(self) -> Any:
        return self._last_lake() if self._last_lake is not None else None

    # ---- Strategy Protocol ---------------------------------------------------

    def fit(self, dataset: Any) -> None:
        self._inner.fit(dataset)

    def extract_features(self, ticker: str, as_of: Any, lake: Any) -> Features:
        return self._inner.extract_features(ticker, as_of, lake)

    # ---- persistence -----------------------------------------------------------

    def save(self, path: Path) -> None:
        super().save(path)
        self._inner.save(Path(path) / INNER_DIR)

    @classmethod
    def load(cls, path: Path) -> InnerStrategyWrapper:
        path = Path(path)
        params = json.loads((path / "params.json").read_text())
        instance = cls(params)
        inner_dir = path / INNER_DIR
        if inner_dir.is_dir():
            inner_cls = import_strategy_class(params["inner_class_path"])
            instance._inner = inner_cls.load(inner_dir)
            instance._adopt_inner()
        return instance
