"""RSI-PCA predictive strategy — inspired by neurotrader888/RSI-PCA.

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

Implementation note (divergence from the reference): the reference
``np.dot(rsis, evecs[j])`` indexes eigenvectors as rows. Since
``numpy.linalg.eigh`` returns eigenvectors as *columns*, that reference
implementation is not a true principal-component projection. Our version
uses ``X @ evecs[:, :n_components]`` — the mathematically correct PCA — so
behavior will differ from the reference on the same input, by design.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from stonks.core.interval import Interval
from stonks.core.params import ParameterSpec
from stonks.core.types import Features, Order, Portfolio
from stonks.features.library import rsi
from stonks.strategies.base import BaseStrategy


class RSIPCAStrategy(BaseStrategy):
    id = "rsi_pca"

    @classmethod
    def parameter_spec(cls):
        return [
            ParameterSpec(
                name="n_components", kind="int", default=3, bounds=(1, 8),
                description="Top-K principal components to keep.",
            ),
            ParameterSpec(
                name="lookahead", kind="int", default=6, bounds=(1, 48),
                description="Forward return horizon (in bars) the linear model predicts.",
            ),
            ParameterSpec(
                name="long_quantile", kind="float", default=0.99, bounds=(0.80, 0.999),
                description="In-sample prediction quantile that triggers a long.",
            ),
            ParameterSpec(
                name="short_quantile", kind="float", default=0.01, bounds=(0.001, 0.20),
                description="In-sample prediction quantile that triggers a short.",
            ),
            ParameterSpec(
                name="rsi_period_min", kind="int", default=2, bounds=(2, 10),
                tunable=False,
                description="Shortest RSI period in the feature matrix (inclusive).",
            ),
            ParameterSpec(
                name="rsi_period_max", kind="int", default=25, bounds=(5, 60),
                tunable=False,
                description="Exclusive upper bound on RSI period.",
            ),
            ParameterSpec(
                name="interval", kind="categorical", default="1d",
                bounds=["1m", "5m", "15m", "30m", "1h", "4h", "12h", "1d", "1w"],
                tunable=False,
                description="Bar interval to read from the lake.",
            ),
            ParameterSpec(
                name="ticker", kind="categorical", default="AAPL.US",
                bounds=None, tunable=False,
                description="Ticker the strategy trades.",
            ),
            ParameterSpec(
                name="allocation", kind="float", default=1.0, bounds=(0.0, 1.0),
                tunable=False,
                description="Fraction of cash deployed on a fresh long entry.",
            ),
        ]

    def __init__(self, params):
        filtered = {k: v for k, v in params.items() if k != "ticker"}
        BaseStrategy.__init__(self, filtered)
        self.params["ticker"] = params.get("ticker", "AAPL.US")
        self._rsi_means: np.ndarray | None = None
        self._evecs: np.ndarray | None = None        # shape (n_rsi, n_components)
        self._coefs: np.ndarray | None = None        # shape (n_components,)
        self._long_thresh: float | None = None
        self._short_thresh: float | None = None

    # ---- Strategy Protocol -------------------------------------------------

    @property
    def is_fitted(self) -> bool:
        return self._coefs is not None

    def fit(self, dataset) -> None:
        ticker = self.params["ticker"]
        interval = Interval.parse(self.params["interval"])
        train_start, train_end = dataset.train_window

        bars = dataset.lake.get_bars(
            ticker, interval,
            start=_to_datetime(train_start),
            end=_to_datetime(train_end),
        )
        if bars.empty:
            raise ValueError(f"no bars for {ticker!r} in training window")

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
        self._evecs = top

        projected = x_c @ top
        coefs, *_ = np.linalg.lstsq(projected, y, rcond=None)
        self._coefs = coefs

        preds = projected @ coefs
        self._long_thresh = float(np.quantile(preds, float(self.params["long_quantile"])))
        self._short_thresh = float(np.quantile(preds, float(self.params["short_quantile"])))

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
        if ticker != self.params["ticker"]:
            return None
        pred = self._predict_current(ticker, as_of, lake)
        if pred is None:
            return None
        if pred > self._long_thresh:
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
                        client_id=f"{self.id}:buy:{target}:{_iso(as_of)}",
                        ticker=target, side="buy", quantity=qty,
                        order_type="market", strategy_id=self.id,
                    )
                )
            return orders

        if not my_picks and holding > 0:
            orders.append(
                Order(
                    client_id=f"{self.id}:sell:{target}:{_iso(as_of)}",
                    ticker=target, side="sell", quantity=holding,
                    order_type="market", strategy_id=self.id,
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
        (Path(path) / "fitted_state.json").write_text(
            json.dumps(state, indent=2, sort_keys=True)
        )

    @classmethod
    def load(cls, path) -> RSIPCAStrategy:
        instance: RSIPCAStrategy = super().load(path)  # type: ignore[assignment]
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

    def _predict_current(self, ticker: str, as_of, lake: Any) -> float | None:
        if not self.is_fitted or lake is None:
            return None
        if ticker != self.params["ticker"]:
            return None

        interval = Interval.parse(self.params["interval"])
        rsi_max = int(self.params["rsi_period_max"])
        span_bars = rsi_max * 4 + 20
        span_td = interval.to_timedelta() * span_bars
        start = _to_datetime(as_of) - span_td

        bars = lake.get_bars(ticker, interval, start=start, end=as_of)
        if bars.empty:
            return None
        closes = bars["close"].astype(float).reset_index(drop=True)

        periods = list(
            range(int(self.params["rsi_period_min"]), int(self.params["rsi_period_max"]))
        )
        current_rsis = np.array([rsi(closes, p).iloc[-1] for p in periods], dtype=float)
        if np.any(np.isnan(current_rsis)):
            return None

        centered = current_rsis - self._rsi_means
        projected = centered @ self._evecs
        return float(projected @ self._coefs)


def _to_datetime(as_of) -> datetime:
    if isinstance(as_of, datetime):
        return as_of
    if isinstance(as_of, date):
        return datetime(as_of.year, as_of.month, as_of.day)
    return as_of


def _iso(as_of) -> str:
    return as_of.isoformat() if hasattr(as_of, "isoformat") else str(as_of)
