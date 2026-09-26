"""RSI-PCA predictive strategy — a classic PCA-over-multiple-RSI-periods
signal with a linear read-out head.

A proper ML strategy that exercises the full ``fit`` → ``save`` → ``load``
lifecycle of our ``BaseStrategy``:

1. Compute the RSI at every integer period in ``[rsi_period_min, rsi_period_max)``.
   Each RSI period is a feature column.
2. Fit PCA on the feature matrix (centered; eigenvectors of the covariance
   matrix, sorted by eigenvalue magnitude).
3. Keep the top ``n_components`` principal components, project the training
   data onto them.
4. Fit a linear regression from the PCs to a forward ``lookahead``-bar
   log-return target.
5. Derive long/short thresholds as quantiles of the in-sample predictions
   (``long_quantile`` and ``short_quantile``).

At inference time we compute the current bar's RSI vector, center with the
fitted means, project onto the fitted eigenvectors, dot into the linear
coefficients, and compare to the thresholds.

Holding period: the model predicts a ``lookahead``-bar return, so a long
entry is held for at least ``hold_bars`` bars (default: ``lookahead``). The
position is long on a bar when the raw long signal fired on any of the last
``hold_bars`` bars, including this one, so a fresh signal restarts the
hold. The original neurotrader888 script gets a similar effect by
smoothing its signal with a rolling mean over ``lookahead`` bars; Stonks is
long-only and binary, so it holds the whole position instead of scaling
it. The hold is replayed from bars, not from what the instance saw earlier:
each of the last ``hold_bars`` predictions is computed exactly as it would
have been on its own bar (memoized per lake), so a freshly loaded instance
gives the same answer as one that walked every bar. ``hold_bars=1`` exits
on the first bar the prediction drops below the threshold.

Implementation note (intentional divergence from the original): PCA is
done the mathematically correct way — eigenvectors come out as the columns
of ``np.linalg.eigh``'s output, so we project via
``X @ evecs[:, :n_components]``. The original script indexes the
eigenvector matrix by row, which is a different, non-PCA projection, so
this port will not reproduce its numbers exactly. That is deliberate.
"""

from __future__ import annotations

import json
import weakref
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from stonks.core.interval import Interval
from stonks.core.params import ParameterSpec
from stonks.core.types import Features, Order, Portfolio
from stonks.features.library import rsi
from stonks.strategies._common import LakeBarCaches, iso
from stonks.strategies.base import BaseStrategy
from stonks.strategies.examples._nt888_common import train_bars


class RSIPCAStrategy(BaseStrategy):
    id = "rsi_pca"
    hypothesis = (
        "The shape of RSI across many periods, compressed with PCA, "
        "carries a small linear signal for the next few bars. Purely "
        "statistical, it fails when that relation drifts."
    )
    alpha_family = "data_driven"
    premise = "none"
    label_horizon_bars = 6
    required_history_bars = 26

    def param_metadata(self) -> dict[str, int]:
        p = self.params
        return {
            "label_horizon_bars": max(int(p["lookahead"]), int(p["hold_bars"])),
            "required_history_bars": int(p["rsi_period_max"]) + 1,
        }

    @classmethod
    def parameter_spec(cls):
        return [
            ParameterSpec(
                name="n_components",
                kind="int",
                default=3,
                bounds=(1, 8),
                description="Top-K principal components to keep.",
            ),
            ParameterSpec(
                name="lookahead",
                kind="int",
                default=6,
                bounds=(1, 48),
                description="Forward return horizon (in bars) the linear model predicts.",
            ),
            ParameterSpec(
                name="hold_bars",
                kind="int",
                default=6,
                bounds=(1, 48),
                description=(
                    "Minimum bars a long is held after its latest signal. "
                    "Defaults to lookahead when not given."
                ),
            ),
            ParameterSpec(
                name="long_quantile",
                kind="float",
                default=0.99,
                bounds=(0.80, 0.999),
                description="In-sample prediction quantile that triggers a long.",
            ),
            ParameterSpec(
                name="short_quantile",
                kind="float",
                default=0.01,
                bounds=(0.001, 0.20),
                description="In-sample prediction quantile that triggers a short.",
            ),
            ParameterSpec(
                name="rsi_period_min",
                kind="int",
                default=2,
                bounds=(2, 10),
                tunable=False,
                description="Shortest RSI period in the feature matrix (inclusive).",
            ),
            ParameterSpec(
                name="rsi_period_max",
                kind="int",
                default=25,
                bounds=(5, 60),
                tunable=False,
                description="Exclusive upper bound on RSI period.",
            ),
            ParameterSpec(
                name="interval",
                kind="categorical",
                default="1d",
                bounds=["1m", "5m", "15m", "30m", "1h", "4h", "12h", "1d", "1w"],
                tunable=False,
                description="Bar interval to read from the lake.",
            ),
            ParameterSpec(
                name="ticker",
                kind="categorical",
                default="AAPL.US",
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

    def __init__(self, params):
        BaseStrategy.__init__(self, params)
        if "hold_bars" not in params:
            self.params["hold_bars"] = int(self.params["lookahead"])
        self._rsi_means: np.ndarray | None = None
        self._evecs: np.ndarray | None = None  # shape (n_rsi, n_components)
        self._coefs: np.ndarray | None = None  # shape (n_components,)
        self._long_thresh: float | None = None
        self._short_thresh: float | None = None
        self._bar_caches = LakeBarCaches()
        # per lake: bar timestamp -> prediction (None when not computable)
        self._preds: weakref.WeakKeyDictionary[Any, dict[Any, float | None]] = (
            weakref.WeakKeyDictionary()
        )

    # ---- Strategy Protocol -------------------------------------------------

    @property
    def is_fitted(self) -> bool:
        return self._coefs is not None

    def fit(self, dataset) -> None:
        ticker = self.params["ticker"]
        interval = Interval.parse(self.params["interval"])
        # RS-12: the adjusted basis the predictions use, and the whole last
        # training day (intraday bars included), like the other nt888 fits.
        bars = train_bars(dataset, ticker, interval, caches=self._bar_caches)

        closes = bars["close"].astype(float).reset_index(drop=True)
        periods = list(
            range(int(self.params["rsi_period_min"]), int(self.params["rsi_period_max"]))
        )
        if len(periods) < 2:
            raise ValueError("rsi_period range must include at least 2 periods")

        rsis = pd.DataFrame({p: rsi(closes, p) for p in periods})
        lookahead = int(self.params["lookahead"])
        target = np.log(closes).diff(lookahead).shift(-lookahead)

        combined = rsis.copy()
        combined["_target"] = target
        combined = combined.dropna()
        if len(combined) < max(20, 2 * lookahead):
            raise ValueError(
                f"insufficient clean training rows ({len(combined)}) — "
                f"expand the training window or reduce lookback/lookahead"
            )

        y = combined["_target"].to_numpy()
        x = combined[periods].to_numpy()

        means = x.mean(axis=0)
        self._rsi_means = means
        x_c = x - means

        cov = np.cov(x_c, rowvar=False)
        evals, evecs = np.linalg.eigh(cov)
        # sort descending by eigenvalue; eigh returns ascending
        order = np.argsort(evals)[::-1]
        evecs = evecs[:, order]

        n_c = int(self.params["n_components"])
        if n_c > evecs.shape[1]:
            n_c = evecs.shape[1]
        top = evecs[:, :n_c]
        # contiguous, like the array load() rebuilds from JSON, so a saved and
        # reloaded instance multiplies in the same order (bit-identical output)
        self._evecs = np.ascontiguousarray(top)

        projected = x_c @ top
        coefs, *_ = np.linalg.lstsq(projected, y, rcond=None)
        self._coefs = coefs

        preds = projected @ coefs
        self._long_thresh = float(np.quantile(preds, float(self.params["long_quantile"])))
        self._short_thresh = float(np.quantile(preds, float(self.params["short_quantile"])))
        self._preds = weakref.WeakKeyDictionary()  # predictions of the old fit are stale

    def extract_features(self, ticker: str, as_of, lake: Any) -> Features:
        pred = self._predict_current(ticker, as_of, lake)
        if pred is None:
            return Features(values={})
        signal = 1.0 if pred > self._long_thresh else (-1.0 if pred < self._short_thresh else 0.0)
        return Features(
            values={
                "pred": pred,
                "long_thresh": float(self._long_thresh),
                "short_thresh": float(self._short_thresh),
                "signal": signal,
            }
        )

    def estimate_return(self, ticker: str, as_of, lake: Any) -> float | None:
        """The newest prediction above the long threshold among the last
        ``hold_bars`` bars (this one included), or ``None`` — see the module
        doc on the holding period."""
        if ticker != self.params["ticker"] or not self.is_fitted or lake is None:
            return None
        interval = Interval.parse(self.params["interval"])
        recent = self._bar_caches.for_lake(lake).last_n_bars(
            ticker, interval, as_of, int(self.params["hold_bars"])
        )
        for ts in reversed(list(recent["timestamp"]) if not recent.empty else []):
            pred = self._predict_at_bar(ticker, ts, lake)
            if pred is not None and pred > self._long_thresh:
                return pred
        return None

    def decide(
        self,
        my_picks: Sequence[tuple[float, str]],
        portfolio: Portfolio,
        prices: Mapping[str, float],
        as_of,
    ) -> list[Order]:
        target = self.params["ticker"]
        price = prices.get(target)
        holding = portfolio.positions.get(target, 0.0)
        orders: list[Order] = []

        if my_picks and price and price > 0 and holding <= 0 and portfolio.cash > 0:
            qty = (portfolio.cash * float(self.params["allocation"])) / price
            if qty > 0:
                orders.append(
                    Order(
                        client_id=f"{self.id}:buy:{target}:{iso(as_of)}",
                        ticker=target,
                        side="buy",
                        quantity=qty,
                        order_type="market",
                        strategy_id=self.id,
                    )
                )
            return orders

        if not my_picks and holding > 0:
            orders.append(
                Order(
                    client_id=f"{self.id}:sell:{target}:{iso(as_of)}",
                    ticker=target,
                    side="sell",
                    quantity=holding,
                    order_type="market",
                    strategy_id=self.id,
                )
            )
        return orders

    # ---- persistence -------------------------------------------------------

    def save(self, path) -> None:
        super().save(path)
        if not self.is_fitted:
            return
        state = {
            "rsi_means": self._rsi_means.tolist(),
            "evecs": self._evecs.tolist(),
            "coefs": self._coefs.tolist(),
            "long_thresh": float(self._long_thresh),
            "short_thresh": float(self._short_thresh),
        }
        (Path(path) / "fitted_state.json").write_text(json.dumps(state, indent=2, sort_keys=True))

    @classmethod
    def load(cls, path) -> RSIPCAStrategy:
        params = json.loads((Path(path) / "params.json").read_text())
        # Artifacts saved before ``hold_bars`` existed exited on the first
        # bar below the threshold; keep that rule (their survival reports
        # were made with it) instead of the lookahead default.
        params.setdefault("hold_bars", 1)
        instance = cls(params)
        state_file = Path(path) / "fitted_state.json"
        if state_file.exists():
            state = json.loads(state_file.read_text())
            instance._rsi_means = np.asarray(state["rsi_means"], dtype=float)
            instance._evecs = np.asarray(state["evecs"], dtype=float)
            instance._coefs = np.asarray(state["coefs"], dtype=float)
            instance._long_thresh = float(state["long_thresh"])
            instance._short_thresh = float(state["short_thresh"])
        return instance

    # ---- internals ---------------------------------------------------------

    def _predict_at_bar(self, ticker: str, bar_ts: Any, lake: Any) -> float | None:
        """``_predict_current`` as of one bar's own timestamp, memoized per
        lake: a backtest asks for the last ``hold_bars`` bars on every bar,
        so each bar's prediction is computed once."""
        try:
            memo = self._preds.setdefault(lake, {})
        except TypeError:  # lake not weakly referenceable: no memo
            return self._predict_current(ticker, bar_ts, lake)
        key = (ticker, bar_ts)
        if key not in memo:
            memo[key] = self._predict_current(ticker, bar_ts, lake)
        return memo[key]

    def _predict_current(self, ticker: str, as_of, lake: Any) -> float | None:
        if not self.is_fitted or lake is None:
            return None
        if ticker != self.params["ticker"]:
            return None

        interval = Interval.parse(self.params["interval"])
        rsi_max = int(self.params["rsi_period_max"])
        # RSI is recursive (Wilder smoothing), so give it a warm-up tail of
        # several periods beyond the longest one.
        closes = pd.Series(
            self._bar_caches.for_lake(lake).last_n_closes(ticker, interval, as_of, rsi_max * 4 + 20)
        )
        if closes.empty:
            return None

        periods = list(
            range(int(self.params["rsi_period_min"]), int(self.params["rsi_period_max"]))
        )
        current_rsis = np.array([rsi(closes, p).iloc[-1] for p in periods], dtype=float)
        if np.any(np.isnan(current_rsis)):
            return None

        centered = current_rsis - self._rsi_means
        projected = centered @ self._evecs
        return float(projected @ self._coefs)
