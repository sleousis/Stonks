"""LearningRanker: a gradient-boosting model that ranks a universe from the
factor dataset (roadmap 23.12).

**Training** (``fit``). For each training window of the lab's dataset (the
one train window, or the purged segments of a CV fold,
``LabDataset.train_windows``):

1. Build the factor dataset (:func:`~stonks.factors.dataset.factor_dataset`)
   over the window with the next-open label ``O[t+1+h] / O[t+1] - 1``, where
   ``h`` is ``horizon_bars``. Bars are read up to the window's last day and
   never later, so the labels of the last ``h + 1`` bars are empty and drop
   out: no label ever reaches past a window, which purges each segment by
   construction (P9, P12).
2. Keep every ``sample_step``-th date, so labels overlap less.
3. Turn each feature into its per-date percentile rank around zero and the
   label into its per-date percentile rank
   (:mod:`stonks.features.ranking`). The model learns the order of next
   period's returns, not their size.
4. Fit the regressor (``model``, a :class:`~stonks.features.ml.Regressor`
   kind, histogram gradient boosting by default) with the params'
   hyperparameters and a fixed seed.

With ``cv_folds >= 2`` the fit also scores the model on purged, embargoed
k-fold out-of-fold predictions: the daily IC, its mean, IR and hit rate,
and each feature's permutation importance. That is a diagnostic only.
Nothing is picked from it: the factor set and every hyperparameter are
params, so each choice is a lab trial counted in the trial ledger (P5).

**Scoring** (``estimate_return``). On the last session of each month in
``rebalance_months`` (the anchor) the model scores the universe from the
factor values known at that anchor, through a point-in-time view at the
anchor when the lake is one. The best ``top_pct`` are held, each with its
percentile rank in ``(0, 1]``; the rest get ``None``. Between anchors the
answer is the last anchor's, so a construction pipeline keeps the book
steady. ``decide`` rebalances equal weight on anchors through the
``equal_weight_top_n`` constructor, and any constructor applies when the
backtest or the tick runs the construction pipeline.

Unfitted, the strategy holds nothing. Daily bars only.

**Lifecycle.** The class overrides ``fit``, so it is ``retrainable``: the
weekly ``model_retrain`` job refits it into a candidate version that runs
as a model book, and a swap goes through governance
(``docs/model-lifecycle.md``).

Persistence: the model under ``model/`` (joblib with a digest check, see
:mod:`stonks.features.ml`) and ``fitted_state.json``. Only load artifact
directories this system wrote.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd

from stonks.core.interval import Interval
from stonks.core.params import ParameterSpec
from stonks.core.types import AssetClass, Features, Order, Portfolio
from stonks.factors.base import Factor
from stonks.factors.dataset import LABEL, factor_dataset
from stonks.factors.engine import PanelRequest
from stonks.factors.expression import ExpressionError
from stonks.factors.registry import resolve_factors
from stonks.features.ml import Regressor, load_regressor, make_regressor, regressor_kinds
from stonks.features.momentum import is_quarter_rebalance_day
from stonks.features.ranking import (
    CVDiagnostic,
    RankerReport,
    cross_sectional_ranks,
    purged_cv_diagnostic,
    rank_label,
)
from stonks.features.sessions import last_session_of_month
from stonks.portfolio.base import ConstructionInput, get_constructor
from stonks.store.pit import PointInTimeLake
from stonks.strategies._common import memo_scope, visible_cutoff
from stonks.strategies.base import BaseStrategy
from stonks.strategies.examples._cross_section import (
    lake_universe,
    orders_from_constructor,
    parse_universe,
    session_cutoff,
)
from stonks.strategies.examples.factor_strategy import select_top

__all__ = ["LearningRanker"]

_MONTHLY = ",".join(str(m) for m in range(1, 13))
_REBALANCE_MONTHS = [_MONTHLY, "3,6,9,12", "2,5,8,11", "1,4,7,10", "6,12", "12"]
_STATE_FILE = "fitted_state.json"
_MODEL_DIR = "model"
#: Labelled rows and dates a fit needs.
_MIN_ROWS = 30
_MIN_DATES = 3
#: Calendar days back from the anchor a ticker's last bar may be.
_MAX_AGE_DAYS = 10
_ANCHORS_KEPT = 4
_ALL_CLASSES: tuple[AssetClass, ...] = ("equity", "crypto", "commodity", "bond")


class LearningRanker(BaseStrategy):
    id = "learning_ranker"
    summary = "A model trained on many factors ranks the universe and buys the top names."
    hypothesis = (
        "Many weak factors, each known before the decision, together rank next "
        "month's returns across a universe better than any one of them, and a "
        "shallow gradient-boosting model can learn how they combine. The top "
        "slice earns that spread net of costs. Fails when the model overfits a "
        "short history, when the factor relationships shift after the training "
        "window, or when the spread is too small to pay for turnover."
    )
    alpha_family = "data_driven"
    premise = "none"
    label_horizon_bars = 22
    required_history_bars = 61
    applicable_asset_classes = _ALL_CLASSES

    def __init__(self, params: Any) -> None:
        super().__init__(params)
        self._factors = _resolve(self.params["factors"])
        if not 0.0 < float(self.params["top_pct"]) <= 1.0:
            raise ValueError("top_pct must be in (0, 1]")
        classes = set.intersection(*(set(f.asset_classes) for f in self._factors))
        self.applicable_asset_classes = tuple(  # type: ignore[assignment]
            c for c in _ALL_CLASSES if c in classes
        ) or ("equity",)
        self._model: Regressor | None = None
        self._state: dict[str, Any] = {}
        self._memo: dict[Any, dict[date, dict[str, float]]] = {}

    def param_metadata(self) -> dict[str, int]:
        try:
            factors = _resolve(self.params["factors"])
        except ValueError:
            return {}  # __init__ reports the bad factor list
        warm = max((f.lookback_bars for f in factors), default=0)
        return {
            "label_horizon_bars": int(self.params["horizon_bars"]) + 1,
            "required_history_bars": warm + 1,
        }

    @classmethod
    def parameter_spec(cls):
        return [
            ParameterSpec(
                name="factors",
                kind="categorical",
                default="alpha158",
                bounds=None,
                tunable=False,
                description="Factor sets, library ids or formulas, comma-separated.",
            ),
            ParameterSpec(
                name="horizon_bars",
                kind="int",
                default=21,
                bounds=(1, 126),
                tunable=False,
                description="Bars the next-open label looks ahead (the period ranked).",
            ),
            ParameterSpec(
                name="sample_step",
                kind="int",
                default=5,
                bounds=(1, 63),
                tunable=False,
                description="Train on every n-th date, so labels overlap less.",
            ),
            ParameterSpec(
                name="top_pct",
                kind="float",
                default=0.2,
                bounds=(0.05, 0.5),
                description="Fraction of the scored universe held.",
            ),
            ParameterSpec(
                name="model",
                kind="categorical",
                default="hist_gbm",
                bounds=regressor_kinds(),
                tunable=False,
                description="Regressor kind behind the model seam.",
            ),
            ParameterSpec(
                name="learning_rate",
                kind="float",
                default=0.05,
                bounds=(0.01, 0.3),
                description="Boosting learning rate.",
            ),
            ParameterSpec(
                name="max_iter",
                kind="int",
                default=200,
                bounds=(10, 2000),
                tunable=False,
                description="Boosting rounds.",
            ),
            ParameterSpec(
                name="max_leaf_nodes",
                kind="int",
                default=15,
                bounds=(4, 63),
                description="Leaves per tree.",
            ),
            ParameterSpec(
                name="min_samples_leaf",
                kind="int",
                default=50,
                bounds=(5, 1000),
                description="Rows a leaf needs.",
            ),
            ParameterSpec(
                name="l2_regularization",
                kind="float",
                default=1.0,
                bounds=(0.0, 10.0),
                tunable=False,
                description="L2 penalty on leaf values.",
            ),
            ParameterSpec(
                name="cv_folds",
                kind="int",
                default=3,
                bounds=(0, 10),
                tunable=False,
                description="Purged k-fold folds of the fit diagnostic (0 or 1 turns it off).",
            ),
            ParameterSpec(
                name="embargo_pct",
                kind="float",
                default=0.01,
                bounds=(0.0, 0.2),
                tunable=False,
                description="Share of rows embargoed after each diagnostic test fold.",
            ),
            ParameterSpec(
                name="rebalance_months",
                kind="categorical",
                default=_MONTHLY,
                bounds=_REBALANCE_MONTHS,
                tunable=False,
                description="Months whose last session rebalances the book.",
            ),
            ParameterSpec(
                name="universe",
                kind="categorical",
                default="",
                bounds=None,
                tunable=False,
                description="Comma-separated tickers ranked together; empty = the lab's or the lake's.",
            ),
            ParameterSpec(
                name="seed",
                kind="int",
                default=0,
                bounds=(0, 2**31 - 1),
                tunable=False,
                description="Model and diagnostic seed (the fit is deterministic for a seed).",
            ),
        ]

    # ---- properties --------------------------------------------------------------

    @property
    def factors(self) -> list[Factor]:
        return list(self._factors)

    @property
    def feature_names(self) -> list[str]:
        return [f.id for f in self._factors]

    @property
    def is_fitted(self) -> bool:
        return self._model is not None

    def fitted_state(self) -> dict[str, Any]:
        return dict(self._state)

    def ranker_report(self) -> RankerReport | None:
        """The fitted model's importance and IC for the report section."""
        return RankerReport.from_state(self._state) if self.is_fitted else None

    # ---- fit -----------------------------------------------------------------------

    def fit(self, dataset: Any) -> None:
        interval = getattr(dataset, "interval", Interval.DAY_1)
        if interval != Interval.DAY_1:
            raise ValueError("the learning ranker trains on daily bars only")
        universe = self._fit_universe(dataset)
        if not universe:
            raise ValueError("no universe to train on")
        windows = getattr(dataset, "train_windows", None) or (dataset.train_window,)
        parts = [self._window_rows(dataset.lake, universe, a, b) for a, b in windows]
        parts = [p for p in parts if p is not None]
        if not parts:
            raise ValueError("no labelled rows in the training window")
        x = np.vstack([p[0] for p in parts])
        y = np.concatenate([p[1] for p in parts])
        dates = pd.DatetimeIndex(np.concatenate([p[2] for p in parts]))
        ends = pd.DatetimeIndex(np.concatenate([p[3] for p in parts]))
        n_dates = dates.nunique()
        if len(y) < _MIN_ROWS or n_dates < _MIN_DATES:
            raise ValueError(
                f"only {len(y)} labelled rows on {n_dates} dates in the training window; "
                f"need {_MIN_ROWS} rows on {_MIN_DATES} dates"
            )
        model = self._new_model()
        model.fit(x, y)
        diag = self._diagnostic(x, y, dates, ends)
        self._model = model
        self._memo = {}
        self._state = {
            "model": model.kind,
            "hyperparameters": model.hyperparameters(),
            "feature_names": self.feature_names,
            "horizon_bars": int(self.params["horizon_bars"]),
            "n_rows": len(y),
            "n_dates": int(n_dates),
            "n_segments": len(parts),
            "n_tickers": len(universe),
            "train_start": _iso_day(dates.min()),
            "train_end": _iso_day(dates.max()),
            "last_label_end": _iso_day(ends.max()),
            "cv_folds": 0,
            "importance": {},
            "ic_by_date": {},
        }
        if diag is not None:
            self._state.update(
                {
                    "cv_folds": int(self.params["cv_folds"]),
                    "ic_mean": _finite(diag.summary["ic_mean"]),
                    "ic_std": _finite(diag.summary["ic_std"]),
                    "ic_ir": _finite(diag.summary["ic_ir"]),
                    "ic_hit_rate": _finite(diag.summary["ic_hit_rate"]),
                    "ic_dates": int(diag.summary["n_dates"]),
                    "importance": {k: _finite(v) or 0.0 for k, v in diag.importance.items()},
                    "ic_by_date": {_iso_day(d): float(v) for d, v in diag.ic_by_date.items()},
                }
            )

    def _new_model(self) -> Regressor:
        p = self.params
        return make_regressor(
            str(p["model"]),
            max_iter=int(p["max_iter"]),
            learning_rate=float(p["learning_rate"]),
            max_leaf_nodes=int(p["max_leaf_nodes"]),
            min_samples_leaf=int(p["min_samples_leaf"]),
            l2_regularization=float(p["l2_regularization"]),
            seed=int(p["seed"]),
        )

    def _diagnostic(
        self, x: np.ndarray, y: np.ndarray, dates: pd.DatetimeIndex, ends: pd.DatetimeIndex
    ) -> CVDiagnostic | None:
        folds = int(self.params["cv_folds"])
        if folds < 2 or dates.nunique() < 2 * folds:
            return None
        return purged_cv_diagnostic(
            x,
            y,
            dates,
            ends,
            self._new_model,
            feature_names=self.feature_names,
            folds=folds,
            embargo_pct=float(self.params["embargo_pct"]),
            seed=int(self.params["seed"]),
        )

    def _window_rows(
        self, lake: Any, universe: Sequence[str], start: date, end: date
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray] | None:
        """``(x, y, decision dates, label end dates)`` of one training
        window, or ``None`` when it has no labelled rows."""
        horizon = int(self.params["horizon_bars"])
        request = PanelRequest(tuple(universe), start, end)
        data = factor_dataset(self._factors, lake, request, label_horizon=horizon)
        if data.empty:
            return None
        data = pd.DataFrame(data.replace([np.inf, -np.inf], np.nan))
        index = cast(pd.MultiIndex, data.index)
        stamps = pd.DatetimeIndex(sorted(index.get_level_values("timestamp").unique()))
        step = int(self.params["sample_step"])
        sampled = stamps[::step]
        data = pd.DataFrame(data[index.get_level_values("timestamp").isin(sampled)])
        x = cross_sectional_ranks(data, self.feature_names)
        labelled = pd.Series(data[LABEL]).notna()
        counts = pd.Series(labelled.groupby(level="timestamp").transform("sum"))
        keep = labelled & (counts >= 2)
        if not keep.any():
            return None
        y = rank_label(pd.Series(data.loc[keep, LABEL]))
        kept = cast(pd.MultiIndex, data.index[keep.to_numpy()])
        dates = pd.DatetimeIndex(kept.get_level_values("timestamp"))
        position = stamps.get_indexer(dates)
        last = len(stamps) - 1
        ends = stamps[np.minimum(position + 1 + horizon, last)]
        return (
            x.loc[keep].to_numpy(dtype=float),
            y.to_numpy(dtype=float),
            dates.to_numpy(dtype="datetime64[ns]"),
            pd.DatetimeIndex(ends).to_numpy(dtype="datetime64[ns]"),
        )

    def _fit_universe(self, dataset: Any) -> list[str]:
        explicit = parse_universe(str(self.params["universe"]))
        if explicit:
            return explicit
        members = [str(t) for t in getattr(dataset, "universe", []) or []]
        if members:
            return list(dict.fromkeys(members))
        return lake_universe(dataset.lake, self.applicable_asset_classes)

    # ---- scoring -------------------------------------------------------------------

    def anchor(self, as_of: Any, lake: Any = None) -> date:
        """The last rebalance session whose bar is complete at a decision
        on ``as_of``: the month-end the held slice was scored on. Behind a
        point-in-time ``lake`` it is never after the view's own daily
        cutoff, so a later ``as_of`` asked by mistake reads no future."""
        seen = visible_cutoff(as_of, Interval.DAY_1).date()
        if isinstance(lake, PointInTimeLake):
            seen = min(seen, lake.bar_cutoff(Interval.DAY_1).date())
        months = {int(m) for m in str(self.params["rebalance_months"]).split(",")}
        year, month = seen.year, seen.month
        for _ in range(25):
            if month in months:
                last = last_session_of_month(year, month)
                if last <= seen:
                    return last
            year, month = (year - 1, 12) if month == 1 else (year, month - 1)
        raise ValueError(f"no rebalance month in {sorted(months)}")

    def score_universe(self, as_of: Any, lake: Any) -> dict[str, float]:
        """``{ticker: percentile}`` of the names held after the anchor of
        ``as_of`` (module doc); empty when unfitted."""
        if self._model is None or lake is None:
            return {}
        anchor = self.anchor(as_of, lake)
        scope = memo_scope(lake)
        try:
            slot = self._memo.setdefault(scope, {})
        except TypeError:  # an unhashable lake: no memo
            return self._score_at(anchor, lake)
        if anchor not in slot:
            if len(slot) >= _ANCHORS_KEPT:
                slot.pop(min(slot))
            slot[anchor] = self._score_at(anchor, lake)
        return slot[anchor]

    def _score_at(self, anchor: date, lake: Any) -> dict[str, float]:
        view = lake
        if isinstance(lake, PointInTimeLake):
            view = lake.pit_session.at(
                datetime.combine(anchor, time()), decision_interval=Interval.DAY_1
            )
        universe = self._universe(view)
        if not universe or self._model is None:
            return {}
        request = PanelRequest(tuple(universe), anchor - timedelta(days=_MAX_AGE_DAYS), anchor)
        data = factor_dataset(self._factors, view, request, label_horizon=None)
        if data.empty:
            return {}
        data = data.replace([np.inf, -np.inf], np.nan)
        latest = data.reset_index().sort_values("timestamp").groupby("ticker").tail(1)
        latest = latest[pd.to_datetime(latest["timestamp"]).dt.date <= anchor]
        if latest.empty:
            return {}
        latest = latest.assign(timestamp=pd.Timestamp(anchor)).set_index(["timestamp", "ticker"])
        x = cross_sectional_ranks(latest, self.feature_names).to_numpy(dtype=float)
        preds = self._model.predict(x)
        tickers = [str(t) for t in latest.index.get_level_values("ticker")]
        values = {t: float(p) for t, p in zip(tickers, preds, strict=True) if math.isfinite(p)}
        return select_top(values, 1, float(self.params["top_pct"]))

    def estimate_return(self, ticker: str, as_of: Any, lake: Any) -> float | None:
        if not self.is_fitted:
            return None
        return self.score_universe(as_of, lake).get(ticker)

    def extract_features(self, ticker: str, as_of: Any, lake: Any) -> Features:
        if not self.is_fitted:
            return Features(values={})
        held = self.score_universe(as_of, lake)
        return Features(
            values={"held": 1.0 if ticker in held else 0.0, "rank": float(held.get(ticker, 0.0))}
        )

    def decide(
        self,
        my_picks: Sequence[tuple[float, str]],
        portfolio: Portfolio,
        prices: Mapping[str, float],
        as_of: Any,
    ) -> list[Order]:
        day, _ = session_cutoff(as_of)
        months = [int(m) for m in str(self.params["rebalance_months"]).split(",")]
        if not is_quarter_rebalance_day(day, months):
            return []
        signals = {t: 1.0 for r, t in my_picks if r > 0}
        inp = ConstructionInput(
            signals={self.id: signals}, portfolio=portfolio, prices=prices, as_of=day
        )
        constructor = get_constructor("equal_weight_top_n", n=max(1, len(signals)))
        return orders_from_constructor(constructor, inp, strategy_id=self.id)

    def _universe(self, lake: Any) -> list[str]:
        explicit = parse_universe(str(self.params["universe"]))
        if explicit:
            return explicit
        return lake_universe(lake, self.applicable_asset_classes)

    # ---- persistence ----------------------------------------------------------------

    def save(self, path: Path) -> None:
        super().save(path)
        if self._model is None:
            return
        path = Path(path)
        self._model.save(path / _MODEL_DIR)
        (path / _STATE_FILE).write_text(json.dumps(self._state, indent=2, sort_keys=True))

    @classmethod
    def load(cls, path: Path) -> LearningRanker:
        path = Path(path)
        instance: LearningRanker = super().load(path)  # type: ignore[assignment]
        state_file = path / _STATE_FILE
        if state_file.exists():
            state = json.loads(state_file.read_text())
            if state.get("feature_names") != instance.feature_names:
                raise ValueError("saved model was trained on different features")
            instance._model = load_regressor(path / _MODEL_DIR)
            instance._state = state
        return instance


def _resolve(spec: Any) -> list[Factor]:
    try:
        factors = resolve_factors(str(spec))
    except (ExpressionError, ValueError) as exc:
        raise ValueError(f"factors {spec!r}: {exc}") from None
    return factors


def _iso_day(stamp: Any) -> str:
    return pd.Timestamp(stamp).date().isoformat()


def _finite(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None
