"""TrendlineMetaLabelStrategy — trendline breakouts filtered by a meta-label model.

Base signal: at bar ``i`` fit support / resistance lines to the log closes
of the previous ``lookback`` bars (``i - lookback .. i - 1``) and project
the resistance to bar ``i``; a close above it opens a base trade (when no
base trade is open). The trade exits at the first close at or beyond
``TP = entry + tp_mult * ATR`` or ``SL = entry - sl_mult * ATR`` (ATR over
``atr_lookback`` bars of log prices, taken at entry), or after
``hold_period`` bars.

``fit(dataset)`` replays the base signal over the train window and builds
one row per base trade: features at entry

- ``resist_slope``: resistance slope / ATR
- ``tl_err``: mean(resistance line - log close) over the window / ATR
- ``max_dist``: max(resistance line - log close) / ATR
- ``volume``: entry-bar volume / rolling median volume (``atr_lookback``)
- ``adx``: ADX(``lookback``) on raw prices

and label ``trade log return > 0``. The base trade is a triple-barrier
label on closes (take profit, stop, time; see :mod:`stonks.features.labels`).
Only trades whose exit bar lies inside the train window are used, so
neither features nor labels read anything after ``train_end``. A random
forest (``n_estimators``, ``max_depth``, fixed ``seed``; behind the
:class:`~stonks.features.ml.Classifier` seam) learns ``P(win)``.

ML hygiene (BL-45):

- A CV fold's dataset has several purged training segments
  (``LabDataset.train_windows``). The base signal is replayed on each
  segment from a flat start, so no trade and no label ever spans a test
  block.
- Each trade is weighted by its average uniqueness
  (:func:`~stonks.features.labels.avg_uniqueness`). Base trades of one
  replay never overlap, so today the weights are all 1. They start to
  matter as soon as labels overlap.
- With ``cv_folds >= 2`` the fit also scores the forest on purged k-fold
  out-of-fold predictions (``cv_accuracy``, ``cv_brier`` in the fitted
  state). This is a diagnostic: the model is still fitted on all trades.
- ``calibration`` (``isotonic`` or ``platt``, roadmap 23.10) maps the
  forest's vote share to a probability, and ``conformal_alpha`` (above 0)
  skips a trade whose conformal prediction set is ambiguous. Both are
  fitted on the purged out-of-fold predictions of the training trades
  (:class:`~stonks.features.calibration.ProbabilityPolicy`), so they need
  ``cv_folds >= 2``. The calibrated probability feeds the threshold and
  bet sizing. ``calibration`` is tunable: each choice tried is a trial.
- The fit keeps the training distribution of each feature
  (:class:`~stonks.features.feature_profile.FeatureProfile`) for the live
  ``feature_drift`` hook, and ``load`` refuses a model whose feature names
  or order differ from today's code.

``estimate_return``: replays the base signal over the last
``3 * hold_period`` bars (flat start, bars ``<= as_of`` only). If a base
trade is open at ``as_of`` and the model's ``P(win)`` for its entry
features exceeds the threshold, returns the model-implied expected log
return ``p * tp_mult * ATR - (1 - p) * sl_mult * ATR`` (floored at a tiny
positive number so an admitted trade is always a pick); otherwise
``None``, which makes ``decide`` exit. Unfitted: always ``None``.

The threshold is the break-even probability of the barriers plus a
margin, ``p* = sl_mult / (tp_mult + sl_mult) + prob_margin``
(:func:`~stonks.features.ml.break_even_probability`), and never below
``prob_thresh``. Below ``p*`` the expected return of a trade is negative.
With ``bet_sizing`` on, a fresh entry deploys ``allocation`` times the
bet size of ``P(win)`` (:func:`~stonks.features.ml.bet_size`), and a
trade whose bet size rounds to zero is skipped.

Persistence: the sklearn model is saved with joblib under ``model/`` (see
:mod:`stonks.features.ml` for the digest check and trust model) plus
``fitted_state.json`` metadata. Only load artifact directories this system
wrote.

Source: ``trendline_break_dataset.py`` / ``walkforward.py`` in
neurotrader888's MIT licensed ``TrendlineBreakoutMetaLabel`` repo. Own
implementation, no code copied. Deviations:

- Indicators (ATR, ADX, volume median) for a bar are computed over a fixed
  tail of ``3 * max(atr_lookback, lookback)`` bars ending at that bar, in
  both fit and inference, so training and live features are identical and
  don't depend on how much history the lake holds (the original ran them
  over the whole series).
- ATR / ADX are Wilder-smoothed (``ewm(alpha=1/n)``) like ``pandas_ta``;
  ADX is implemented here rather than via ``pandas_ta``.
- A single fit on the lab's train window replaces the original's rolling
  walk-forward retrain (the lab's walk-forward folds do that job).
- Run-time trade state is re-derived from a flat start ``3 * hold_period``
  bars back instead of carried across the whole history.
- The threshold is the barriers' break-even probability plus a margin
  (the original hard-coded 0.5) and the forest defaults to 300 trees
  (the original used 1000).
- Long-only, single ticker.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from stonks.core.forecasts import ProbabilityForecast
from stonks.core.interval import Interval
from stonks.core.params import ParameterSpec
from stonks.core.types import Features, Order, Portfolio
from stonks.features.calibration import ProbabilityPolicy, purged_oof_proba
from stonks.features.feature_profile import FeatureProfile, check_feature_schema
from stonks.features.indicators import atr, true_range
from stonks.features.labels import avg_uniqueness
from stonks.features.library import fit_trendlines_single
from stonks.features.ml import ForestClassifier, bet_size, break_even_probability
from stonks.lab.importance import TrainingSet
from stonks.strategies._common import LakeBarCaches, iso
from stonks.strategies._wrapping import INTERVALS
from stonks.strategies.base import BaseStrategy
from stonks.strategies.examples._nt888_common import long_only_decide, train_bar_segments

FEATURE_NAMES = ("resist_slope", "tl_err", "max_dist", "volume", "adx")
_STATE_FILE = "fitted_state.json"
_MODEL_DIR = "model"
_MIN_TRADES = 5
_MEMO_MAX = 50_000


def adx(high: pd.Series, low: pd.Series, close: pd.Series, lookback: int) -> pd.Series:
    """Average directional index (Wilder). Causal; NaN during warm-up."""
    if lookback < 1:
        raise ValueError(f"lookback must be >= 1, got {lookback}")
    high = pd.Series(high, dtype=float).reset_index(drop=True)
    low = pd.Series(low, dtype=float).reset_index(drop=True)
    close = pd.Series(close, dtype=float).reset_index(drop=True)
    up = high.diff()
    down = -low.diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    plus_dm[up.isna()] = np.nan
    minus_dm[down.isna()] = np.nan

    def rma(s: pd.Series) -> pd.Series:
        return s.ewm(alpha=1.0 / lookback, min_periods=lookback).mean()

    tr = rma(true_range(high, low, close))
    plus_di = 100.0 * rma(plus_dm) / tr
    minus_di = 100.0 * rma(minus_dm) / tr
    denom = plus_di + minus_di
    dx = 100.0 * (plus_di - minus_di).abs() / denom.where(denom > 0)
    return rma(dx)


@dataclass(frozen=True)
class BaseTrade:
    entry_ts: pd.Timestamp
    entry_log_price: float
    atr: float
    features: tuple[float, ...]
    exit_ts: pd.Timestamp | None = None
    exit_log_price: float | None = None

    @property
    def label(self) -> bool:
        return self.exit_log_price is not None and self.exit_log_price > self.entry_log_price


class TrendlineMetaLabelStrategy(BaseStrategy):
    id = "trendline_meta_label"
    title = "Filtered trendline breakout"
    summary = (
        "Takes trendline breakouts only when a model trained on past breakouts rates them well."
    )
    hypothesis = (
        "Trendline breakouts work only in some conditions. A model "
        "trained on past breakouts can tell good ones from bad and skip "
        "the bad. Fails when the model is overfit or conditions change."
    )
    alpha_family = "data_driven"
    premise = "trend"
    label_horizon_bars = 12
    #: ``feature_tail + 1`` with the defaults: open_trade needs that many.
    required_history_bars = 505

    def param_metadata(self) -> dict[str, int]:
        p = self.params
        return {
            "label_horizon_bars": int(p["hold_period"]),
            "required_history_bars": 3 * max(int(p["lookback"]), int(p["atr_lookback"])) + 1,
        }

    applicable_asset_classes = ("crypto", "equity")

    @classmethod
    def parameter_spec(cls):
        return [
            ParameterSpec(
                name="lookback",
                kind="int",
                default=72,
                bounds=(24, 300),
                description="Bars the trendlines are fitted over (and ADX period).",
            ),
            ParameterSpec(
                name="hold_period",
                kind="int",
                default=12,
                bounds=(4, 48),
                description="Max bars a base trade is held.",
            ),
            ParameterSpec(
                name="tp_mult",
                kind="float",
                default=3.0,
                bounds=(1, 6),
                description="Take profit in ATRs.",
            ),
            ParameterSpec(
                name="sl_mult",
                kind="float",
                default=3.0,
                bounds=(1, 6),
                description="Stop loss in ATRs.",
            ),
            ParameterSpec(
                name="atr_lookback",
                kind="int",
                default=168,
                bounds=(50, 400),
                description="ATR / volume-median period.",
            ),
            ParameterSpec(
                name="prob_thresh",
                kind="float",
                default=0.0,
                bounds=(0.0, 0.8),
                description="Floor on the P(win) threshold (the threshold is the "
                "break-even probability plus prob_margin, and never below this).",
            ),
            ParameterSpec(
                name="prob_margin",
                kind="float",
                default=0.0,
                bounds=(0.0, 0.2),
                description="Added to the break-even probability sl / (tp + sl).",
            ),
            ParameterSpec(
                name="bet_sizing",
                kind="bool",
                default=False,
                tunable=False,
                description="Scale fresh entries by the bet size of P(win).",
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
                name="calibration",
                kind="categorical",
                default="none",
                bounds=["none", "isotonic", "platt"],
                description="How P(win) is calibrated on purged out-of-fold "
                "predictions before the threshold and bet sizing.",
            ),
            ParameterSpec(
                name="conformal_alpha",
                kind="float",
                default=0.0,
                bounds=(0.0, 0.5),
                tunable=False,
                description="Conformal miss rate. Above 0, a trade whose "
                "prediction set holds both outcomes is skipped (0 turns it off).",
            ),
            ParameterSpec(
                name="n_estimators",
                kind="int",
                default=300,
                bounds=(10, 2000),
                tunable=False,
                description="Trees in the random forest.",
            ),
            ParameterSpec(
                name="max_depth",
                kind="int",
                default=3,
                bounds=(1, 16),
                tunable=False,
                description="Max tree depth.",
            ),
            ParameterSpec(
                name="seed",
                kind="int",
                default=0,
                bounds=(0, 2**31 - 1),
                tunable=False,
                description="Random-forest seed (fit is deterministic for a seed).",
            ),
            ParameterSpec(
                name="interval",
                kind="categorical",
                default="1d",
                bounds=list(INTERVALS),
                tunable=False,
                description="Bar interval to read from the lake.",
            ),
            ParameterSpec(
                name="ticker",
                kind="categorical",
                default="BTC-USD.CC",
                bounds=None,
                tunable=False,
                description="Ticker the strategy trades.",
            ),
            ParameterSpec(
                name="allocation",
                kind="float",
                default=1.0,
                bounds=(0.0, 1.0),
                tunable=False,
                description="Fraction of cash deployed on a fresh long entry.",
            ),
        ]

    def __init__(self, params: Any) -> None:
        super().__init__(params)
        self._classifier: Any = None
        self._policy: ProbabilityPolicy | None = None
        self._profile: FeatureProfile | None = None
        self._state: dict[str, Any] = {}
        self.training_trades: list[BaseTrade] = []
        self._bar_caches = LakeBarCaches()
        self._line_memo: dict[bytes, tuple[float, float] | None] = {}
        self._prob_memo: dict[tuple[float, ...], float] = {}
        self._sizes: dict[str, float] = {}

    # ---- geometry ----------------------------------------------------------------

    @property
    def feature_tail(self) -> int:
        """Bars (ending at a bar) its indicators are computed over."""
        return 3 * max(int(self.params["atr_lookback"]), int(self.params["lookback"]))

    def _resistance(self, window: np.ndarray) -> tuple[float, float] | None:
        """(slope, intercept) of the resistance line fitted to ``window``."""
        key = window.tobytes()
        if key in self._line_memo:
            return self._line_memo[key]
        result: tuple[float, float] | None
        try:
            _support, (slope, intercept) = fit_trendlines_single(window)
            result = (float(slope), float(intercept))
        except (ValueError, np.linalg.LinAlgError):
            result = None
        if len(self._line_memo) > _MEMO_MAX:
            self._line_memo.clear()
        self._line_memo[key] = result
        return result

    def _entry_features(
        self, bars: pd.DataFrame, i: int, line: tuple[float, float]
    ) -> tuple[float, tuple[float, ...]] | None:
        """(ATR, features) at bar ``i`` from the ``feature_tail`` bars ending
        there; ``None`` if any is undefined."""
        lookback = int(self.params["lookback"])
        atr_lb = int(self.params["atr_lookback"])
        tail = bars.iloc[i - self.feature_tail + 1 : i + 1]
        high = tail["high"].astype(float).reset_index(drop=True)
        low = tail["low"].astype(float).reset_index(drop=True)
        close = tail["close"].astype(float).reset_index(drop=True)
        volume = pd.to_numeric(tail["volume"], errors="coerce").reset_index(drop=True)
        atr_now = float(atr(np.log(high), np.log(low), np.log(close), atr_lb).iloc[-1])
        if not atr_now > 0:
            return None
        slope, intercept = line
        window = np.log(close.to_numpy()[-lookback - 1 : -1])
        diffs = intercept + slope * np.arange(lookback) - window
        vol_median = float(volume.iloc[-atr_lb:].median())
        vol_ratio = float(volume.iloc[-1]) / vol_median if vol_median > 0 else float("nan")
        adx_now = float(adx(high, low, close, lookback).iloc[-1])
        features = (
            slope / atr_now,
            float(diffs.mean()) / atr_now,
            float(diffs.max()) / atr_now,
            vol_ratio,
            adx_now,
        )
        if not all(np.isfinite(features)):
            return None
        return atr_now, features

    def _simulate(self, bars: pd.DataFrame) -> tuple[list[BaseTrade], BaseTrade | None]:
        """Replay the base signal over ``bars`` from a flat start. Returns
        the completed trades and the trade still open at the last bar."""
        lookback = int(self.params["lookback"])
        hold = int(self.params["hold_period"])
        tp_mult = float(self.params["tp_mult"])
        sl_mult = float(self.params["sl_mult"])
        log_close = np.log(bars["close"].astype(float).to_numpy())
        timestamps = pd.to_datetime(bars["timestamp"])
        done: list[BaseTrade] = []
        open_trade: BaseTrade | None = None
        entry_i = 0
        tp = sl = 0.0
        for i in range(max(self.feature_tail - 1, lookback), len(bars)):
            if open_trade is None:
                line = self._resistance(log_close[i - lookback : i])
                if line is not None and log_close[i] > line[1] + lookback * line[0]:
                    got = self._entry_features(bars, i, line)
                    if got is not None:
                        atr_now, features = got
                        open_trade = BaseTrade(
                            entry_ts=timestamps.iloc[i],
                            entry_log_price=float(log_close[i]),
                            atr=atr_now,
                            features=features,
                        )
                        entry_i = i
                        tp = log_close[i] + tp_mult * atr_now
                        sl = log_close[i] - sl_mult * atr_now
            if open_trade is not None and (
                log_close[i] >= tp or log_close[i] <= sl or i >= entry_i + hold
            ):
                done.append(
                    BaseTrade(
                        **{
                            **open_trade.__dict__,
                            "exit_ts": timestamps.iloc[i],
                            "exit_log_price": float(log_close[i]),
                        }
                    )
                )
                open_trade = None
        return done, open_trade

    # ---- fit ------------------------------------------------------------------------

    @property
    def is_fitted(self) -> bool:
        return self._classifier is not None

    def _training_trades(self, dataset: Any) -> tuple[list[BaseTrade], list[tuple[int, int]], int]:
        """The completed base trades of every training segment, their bar
        spans (positions across the segments) and the segment count."""
        interval = Interval.parse(self.params["interval"])
        segments = train_bar_segments(
            dataset, self.params["ticker"], interval, caches=self._bar_caches
        )
        # One replay per training segment, completed trades only: a trade
        # still open at a segment's last bar would need bars after it (a
        # test block or the validation window) for its label.
        trades: list[BaseTrade] = []
        spans: list[tuple[int, int]] = []
        offset = 0
        for bars in segments:
            done, _still_open = self._simulate(bars)
            stamps = pd.to_datetime(bars["timestamp"]).tolist()
            where = {ts: i for i, ts in enumerate(stamps)}
            spans.extend((offset + where[t.entry_ts], offset + where[t.exit_ts]) for t in done)
            trades.extend(done)
            offset += len(bars)
        if len(trades) < _MIN_TRADES:
            raise ValueError(
                f"only {len(trades)} complete base trades in the training window; "
                f"need at least {_MIN_TRADES}"
            )
        return trades, spans, len(segments)

    def training_set(self, dataset: Any) -> TrainingSet:
        """The training trades as a :class:`~stonks.lab.importance.TrainingSet`
        (training window only), read by ``stonks lab importance``."""
        trades, spans, _ = self._training_trades(dataset)
        return TrainingSet(
            x=np.array([t.features for t in trades], dtype=float),
            y=np.array([t.label for t in trades], dtype=int),
            t0=np.array([t.entry_ts for t in trades], dtype="datetime64[ns]"),
            t1=np.array([t.exit_ts for t in trades], dtype="datetime64[ns]"),
            feature_names=FEATURE_NAMES,
            sample_weight=avg_uniqueness([a for a, _ in spans], [b for _, b in spans]),
        )

    def fit(self, dataset: Any) -> None:
        trades, spans, n_segments = self._training_trades(dataset)
        x = np.array([t.features for t in trades], dtype=float)
        y = np.array([t.label for t in trades], dtype=int)
        weights = avg_uniqueness([a for a, _ in spans], [b for _, b in spans])
        diagnostic, oof = self._cv_diagnostic(trades, x, y, weights)
        policy = self._fit_policy(oof, y, weights)
        clf = self.new_classifier()
        clf.fit(x, y, sample_weight=weights)
        self._classifier = clf
        self._policy = policy
        self._profile = FeatureProfile.build(x, FEATURE_NAMES)
        self._prob_memo = {}
        self._sizes = {}
        self.training_trades = trades
        self._state = {
            "feature_names": list(FEATURE_NAMES),
            "n_trades": len(trades),
            "n_segments": n_segments,
            "win_rate": float(y.mean()),
            "mean_uniqueness": float(weights.mean()),
            "first_entry": trades[0].entry_ts.isoformat(),
            "last_exit": trades[-1].exit_ts.isoformat(),
            **diagnostic,
        }
        if policy is not None:
            self._state["probability"] = policy.to_dict()

    def _fit_policy(
        self, oof: np.ndarray | None, y: np.ndarray, weights: np.ndarray
    ) -> ProbabilityPolicy | None:
        """The calibration and abstention policy, fitted on the purged
        out-of-fold predictions (``None`` when both are off)."""
        kind = str(self.params["calibration"])
        alpha = float(self.params["conformal_alpha"])
        if kind == "none" and alpha <= 0:
            return None
        if oof is None:
            raise ValueError(
                "calibration and conformal abstention need purged out-of-fold "
                "predictions: set cv_folds >= 2 and train on at least 2 * cv_folds trades"
            )
        return ProbabilityPolicy.fit(
            kind, oof, y, conformal_alpha=alpha if alpha > 0 else None, sample_weight=weights
        )

    def new_classifier(self) -> ForestClassifier:
        """A fresh, unfitted forest with this strategy's settings."""
        return ForestClassifier(
            n_estimators=int(self.params["n_estimators"]),
            max_depth=int(self.params["max_depth"]),
            seed=int(self.params["seed"]),
        )

    def _cv_diagnostic(
        self, trades: list[BaseTrade], x: np.ndarray, y: np.ndarray, weights: np.ndarray
    ) -> tuple[dict[str, float], np.ndarray | None]:
        """Purged k-fold out-of-fold accuracy and Brier score of the
        forest over the training trades, and the out-of-fold predictions
        (empty and ``None`` when turned off or when there are too few
        trades)."""
        folds = int(self.params["cv_folds"])
        if folds < 2 or len(trades) < 2 * folds:
            return {}, None
        t0 = np.array([t.entry_ts for t in trades], dtype="datetime64[ns]")
        t1 = np.array([t.exit_ts for t in trades], dtype="datetime64[ns]")
        oof = purged_oof_proba(
            self.new_classifier, x, y, t0, t1, folds=folds, sample_weight=weights
        )
        scored = np.isfinite(oof)
        if not scored.any():
            return {}, None
        hits = (oof[scored] > 0.5).astype(int) == y[scored]
        return {
            "cv_folds": float(folds),
            "cv_accuracy": float(hits.mean()),
            "cv_brier": float(np.mean((oof[scored] - y[scored]) ** 2)),
        }, oof

    def fitted_state(self) -> dict[str, Any]:
        return dict(self._state)

    def feature_profile(self) -> FeatureProfile | None:
        """The training distribution of each feature (``None`` unfitted)."""
        return self._profile

    def model_feature_row(self, ticker: str, as_of: Any, lake: Any) -> dict[str, float] | None:
        """The model's input row at ``as_of`` in training order, or ``None``
        when there is no base trade to score (read by ``feature_drift``)."""
        trade = self.open_trade(ticker, as_of, lake)
        if trade is None:
            return None
        return dict(zip(FEATURE_NAMES, trade.features, strict=True))

    def predict_proba(self, x: np.ndarray) -> np.ndarray:
        if self._classifier is None:
            raise RuntimeError("strategy is not fitted")
        return np.asarray(self._classifier.predict_proba(np.atleast_2d(x)), dtype=float)

    # ---- inference ----------------------------------------------------------------

    def open_trade(self, ticker: str, as_of: Any, lake: Any) -> BaseTrade | None:
        """The base trade open at ``as_of`` (bars ``<= as_of`` only)."""
        if lake is None or ticker != self.params["ticker"]:
            return None
        interval = Interval.parse(self.params["interval"])
        n = self.feature_tail + 3 * int(self.params["hold_period"])
        bars = self._bar_caches.for_lake(lake).last_n_bars(ticker, interval, as_of, n)
        if len(bars) < self.feature_tail + 1:
            return None
        _done, still_open = self._simulate(bars)
        return still_open

    # ---- probability forecasts (roadmap 23.9, lifecycle calibration) -----------

    def forecast_probability(
        self, ticker: str, as_of: Any, lake: Any
    ) -> ProbabilityForecast | None:
        """``P(win)`` of the base trade open at ``as_of``, keyed by its entry."""
        if not self.is_fitted:
            return None
        trade = self.open_trade(ticker, as_of, lake)
        if trade is None:
            return None
        p = min(max(self._probability(trade.features), 0.0), 1.0)
        return ProbabilityForecast(event_key=iso(trade.entry_ts), probability=p)

    def forecast_outcome(self, ticker: str, event_key: str, as_of: Any, lake: Any) -> bool | None:
        """Whether the base trade that entered at ``event_key`` closed above
        its entry (its meta-label), once it has closed by ``as_of``."""
        if lake is None or ticker != self.params["ticker"]:
            return None
        interval = Interval.parse(self.params["interval"])
        n = self.feature_tail + 3 * int(self.params["hold_period"])
        bars = self._bar_caches.for_lake(lake).last_n_bars(ticker, interval, as_of, n)
        if len(bars) < self.feature_tail + 1:
            return None
        done, _still_open = self._simulate(bars)
        for trade in done:
            if iso(trade.entry_ts) == event_key:
                return trade.label
        return None

    def _raw_probability(self, features: tuple[float, ...]) -> float:
        p = self._prob_memo.get(features)
        if p is None:
            p = float(self.predict_proba(np.array([features]))[0])
            if len(self._prob_memo) > _MEMO_MAX:
                self._prob_memo.clear()
            self._prob_memo[features] = p
        return p

    def _decision(self, features: tuple[float, ...]) -> tuple[float, bool]:
        """(calibrated ``P(win)``, abstain) for a base trade's features."""
        raw = self._raw_probability(features)
        if self._policy is None:
            return raw, False
        decision = self._policy.apply(np.array([raw]))
        return float(decision.probability[0]), bool(decision.abstain[0])

    def _probability(self, features: tuple[float, ...]) -> float:
        return self._decision(features)[0]

    @property
    def threshold(self) -> float:
        """``P(win)`` a base entry must exceed: the barriers' break-even
        probability plus ``prob_margin``, never below ``prob_thresh``."""
        p = self.params
        p_star = break_even_probability(float(p["tp_mult"]), float(p["sl_mult"]))
        return max(float(p["prob_thresh"]), p_star + float(p["prob_margin"]))

    def estimate_return(self, ticker: str, as_of: Any, lake: Any) -> float | None:
        if not self.is_fitted:
            return None
        trade = self.open_trade(ticker, as_of, lake)
        if trade is None:
            return None
        p, abstain = self._decision(trade.features)
        if abstain or not p > self.threshold:
            return None
        if self.params["bet_sizing"]:
            size = float(bet_size(p))
            if size <= 0:
                return None
            if len(self._sizes) > _MEMO_MAX:
                self._sizes.clear()
            self._sizes[iso(as_of)] = size
        expected = trade.atr * (
            p * float(self.params["tp_mult"]) - (1 - p) * float(self.params["sl_mult"])
        )
        return max(expected, 1e-6)

    def extract_features(self, ticker: str, as_of: Any, lake: Any) -> Features:
        trade = self.open_trade(ticker, as_of, lake)
        if trade is None:
            return Features(values={"base_in_trade": 0.0})
        values = {"base_in_trade": 1.0, **dict(zip(FEATURE_NAMES, trade.features, strict=True))}
        if self.is_fitted:
            values["p_win"] = self._probability(trade.features)
        return Features(values=values)

    def decide(
        self,
        my_picks: Sequence[tuple[float, str]],
        portfolio: Portfolio,
        prices: Mapping[str, float],
        as_of: Any,
    ) -> list[Order]:
        allocation = float(self.params["allocation"])
        if self.params["bet_sizing"]:
            allocation *= self._sizes.get(iso(as_of), 1.0)
        return long_only_decide(
            self.id,
            self.params["ticker"],
            allocation,
            my_picks,
            portfolio,
            prices,
            as_of,
        )

    # ---- persistence --------------------------------------------------------------

    def save(self, path: Path) -> None:
        super().save(path)
        if not self.is_fitted:
            return
        path = Path(path)
        self._classifier.save(path / _MODEL_DIR)
        (path / _STATE_FILE).write_text(json.dumps(self._state, indent=2, sort_keys=True))
        if self._profile is not None:
            self._profile.save(path)

    @classmethod
    def load(cls, path: Path) -> TrendlineMetaLabelStrategy:
        path = Path(path)
        instance: TrendlineMetaLabelStrategy = super().load(path)  # type: ignore[assignment]
        state_file = path / _STATE_FILE
        if state_file.exists():
            state = json.loads(state_file.read_text())
            check_feature_schema(state.get("feature_names") or [], FEATURE_NAMES)
            profile = FeatureProfile.load_if_present(path)
            if profile is not None:
                check_feature_schema(profile.names, FEATURE_NAMES)
            instance._classifier = ForestClassifier.load(path / _MODEL_DIR)
            instance._state = state
            instance._profile = profile
            if state.get("probability") is not None:
                instance._policy = ProbabilityPolicy.from_dict(state["probability"])
        return instance
