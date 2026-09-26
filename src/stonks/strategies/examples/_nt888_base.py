"""Shared scaffolding for the single-ticker, long/flat strategies ported
from neurotrader888's indicator research.

Every one of them trades one ``ticker`` at one ``interval`` and turns an
indicator into a long (1) or flat (0) signal. Subclasses implement
:meth:`_evaluate`, which returns the indicator's current state (always
including ``signal`` and ``score``) or ``None`` while there isn't enough
history. The base turns that into the Strategy Protocol the house way
(see ``donchian_breakout``): ``estimate_return`` is a positive score while
long and ``None`` otherwise, and ``decide`` buys when picked and flat and
sells everything when not picked.

The broker is long-only, so wherever an original strategy goes short the
port goes flat.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from stonks.core.interval import Interval
from stonks.core.params import ParameterSpec
from stonks.core.types import AssetClass, Features, Order, Portfolio
from stonks.strategies._common import BarCache, LakeBarCaches, long_only_decide
from stonks.strategies.base import BaseStrategy

#: Floor for ``estimate_return`` while long, so a long signal always clears
#: the backtester's default ``threshold=0`` even when the magnitude proxy
#: happens to be 0 or negative.
MIN_SCORE = 1e-6

INTERVALS = ["1m", "5m", "15m", "30m", "1h", "4h", "12h", "1d", "1w"]


def common_specs(default_ticker: str) -> list[ParameterSpec]:
    """``interval`` / ``ticker`` / ``allocation`` — shared, never tuned."""
    return [
        ParameterSpec(
            name="interval",
            kind="categorical",
            default="1h",
            bounds=INTERVALS,
            tunable=False,
            description="Bar interval to read from the lake. The original research "
            "used hourly bars; every bar-count parameter is in bars of this interval.",
        ),
        ParameterSpec(
            name="ticker",
            kind="categorical",
            default=default_ticker,
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


class SingleTickerLongFlat(BaseStrategy):
    id: ClassVar[str] = "nt888_single_ticker"
    applicable_asset_classes: ClassVar[tuple[AssetClass, ...]] = ("crypto", "equity")

    def __init__(self, params: Any) -> None:
        super().__init__(params)
        self._bar_caches = LakeBarCaches()

    # ---- subclass hook ------------------------------------------------------

    def _evaluate(self, cache: BarCache, ticker: str, as_of: Any) -> dict[str, float] | None:
        """Indicator state at ``as_of`` from bars ``<= as_of`` only, with
        at least ``signal`` (1.0 long / 0.0 flat) and ``score`` (the
        magnitude proxy used while long). ``None`` = not enough history."""
        raise NotImplementedError

    @property
    def interval(self) -> Interval:
        return Interval.parse(self.params["interval"])

    # ---- Strategy Protocol --------------------------------------------------

    def _state(self, ticker: str, as_of: Any, lake: Any) -> dict[str, float] | None:
        if lake is None:
            return None
        return self._evaluate(self._bar_caches.for_lake(lake), ticker, as_of)

    def extract_features(self, ticker: str, as_of: Any, lake: Any) -> Features:
        state = self._state(ticker, as_of, lake)
        return Features(values=dict(state) if state else {})

    def estimate_return(self, ticker: str, as_of: Any, lake: Any) -> float | None:
        if ticker != self.params["ticker"]:
            return None
        state = self._state(ticker, as_of, lake)
        if state is None or state["signal"] <= 0:
            return None
        score = state["score"]
        return max(float(score), MIN_SCORE) if score == score else MIN_SCORE

    def decide(
        self,
        my_picks: Sequence[tuple[float, str]],
        portfolio: Portfolio,
        prices: Mapping[str, float],
        as_of: Any,
    ) -> list[Order]:
        return long_only_decide(
            self.id,
            self.params["ticker"],
            float(self.params["allocation"]),
            my_picks,
            portfolio,
            prices,
            as_of,
        )
