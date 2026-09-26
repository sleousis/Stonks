"""Shared plumbing for strategies that wrap an inner strategy.

Mirrors :class:`stonks.strategies.macro_regime.MacroRegimeFilter`: the
wrapper's params carry ``inner_class_path`` + ``inner_params`` (fully
resolved after construction), the wrapper adopts the inner strategy's
``applicable_asset_classes``, and ``save`` writes the inner strategy into
an ``inner/`` sub-directory so its fitted state survives a registry
round-trip.
"""

from __future__ import annotations

import json
import weakref
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar, get_args

from stonks.core.params import ParameterSpec
from stonks.core.protocols import Strategy
from stonks.core.types import AssetClass, Features
from stonks.strategies.base import BaseStrategy
from stonks.strategies.macro_regime import _import_strategy_class

INNER_DIR = "inner"
INTERVALS = ["1m", "5m", "15m", "30m", "1h", "4h", "12h", "1d", "1w"]


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
    # Instances mirror their inner strategy; the class default admits all.
    applicable_asset_classes: tuple[AssetClass, ...] = get_args(AssetClass)

    def __init__(self, params: Any) -> None:
        super().__init__(params)
        inner_params = self.params["inner_params"]
        if not isinstance(inner_params, Mapping):
            raise ValueError(f"inner_params must be a mapping, got {type(inner_params).__name__}")
        inner_cls = _import_strategy_class(self.params["inner_class_path"])
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

    @property
    def inner(self) -> Strategy:
        return self._inner

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
            inner_cls = _import_strategy_class(params["inner_class_path"])
            instance._inner = inner_cls.load(inner_dir)
            instance._adopt_inner()
        return instance
