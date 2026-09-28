"""RuleStrategy — a strategy defined entirely by a declarative JSON spec.

``params = {"spec": <RuleSpec JSON>}``. The spec (see
:mod:`stonks.strategies.rules.spec`) names indicators from a closed
registry, entry / exit conditions as boolean trees over them, a ranking
indicator, sizing and optional percentage stops. The spec is validated on
construction and stored in its normalized form in ``params``, so the
registry's JSON save / load round-trips it unchanged.

Per bar:

- ``estimate_return(ticker)`` reads the last bars with ``timestamp <=
  as_of`` (through the shared, look-ahead-safe :class:`BarCache`),
  evaluates entry and exit, and returns a positive, rank-preserving score
  when entry holds and exit doesn't (``None`` otherwise).
- ``decide`` first sells held tickers whose exit condition was true on
  this bar, that no longer pass entry (when ``exit_when_entry_false``) or
  that hit a stop-loss / take-profit, then buys the best-ranked picks into
  free slots, equal weight, never spending more than the cash on hand.

Stop-loss / take-profit compare the latest close with the price seen when
this instance decided the buy. The trailing stops (vol or ATR multiple)
sit below the highest close since that buy and never move down; their
width comes from the bars ``estimate_return`` read on the same bar.

The reference lives in memory, but a production tick loads a fresh
instance every time (RS-13). When a held ticker has no reference, ``decide``
rebuilds it from the bars: it replays the spec's signal (entry true, exit
false) over the last :data:`REPLAY_BARS` bars before this one, takes the
close where the latest signal run started as the entry price, and replays
the high-water mark and the trailing stop from there. The replay cannot see
sizing, so when the buy waited for a free slot the rebuilt entry is the
start of the signal run, not the later buy. A run older than the replay
window starts at the window's first bar. The exit *condition* is only known
for tickers this instance evaluated on the same ``as_of``; production ticks
evaluate the whole universe first, so held universe tickers are covered.
"""

from __future__ import annotations

import json
import math
import weakref
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, ClassVar

from stonks.core.corporate_actions import Split
from stonks.core.interval import Interval
from stonks.core.params import ParameterSpec, Params, ParamSpace
from stonks.core.types import AssetClass, Order, Portfolio
from stonks.features.volatility import periods_per_year
from stonks.store.corporate_actions import LakeCorporateActions
from stonks.strategies._common import LakeBarCaches, as_datetime, iso
from stonks.strategies.base import BaseStrategy
from stonks.strategies.rules.interpreter import evaluate, snapshot, stop_width, window_size
from stonks.strategies.rules.spec import RuleSpec, validate_spec
from stonks.strategies.rules.templates import SMA_TREND_FOLLOWING

RULE_STRATEGY_CLASS_PATH = "stonks.strategies.rule_based:RuleStrategy"

_ALL_ASSET_CLASSES: tuple[AssetClass, ...] = ("equity", "crypto", "commodity", "bond")

#: Bars replayed to rebuild a held position's entry and trailing stop.
REPLAY_BARS = 252


@dataclass(frozen=True)
class _Evaluation:
    entry: bool
    exit: bool
    #: Trailing-stop distance below the high-water mark on this bar.
    stop_width: float | None = None


def _rank_score(value: float, order: str) -> float:
    """Map any real ranking value onto (0, inf), strictly increasing in the
    preferred direction and without saturating: ``exp(asinh(x))``."""
    x = value if order == "desc" else -value
    root = math.sqrt(x * x + 1.0)
    return x + root if x >= 0 else 1.0 / (root - x)


class RuleStrategy(BaseStrategy):
    """A declarative rule strategy (indicators, entry / exit conditions,
    ranking, sizing) defined by a validated JSON spec."""

    id = "rule_based"
    title = "Studio rules"
    summary = "Buy and sell rules you build in the Studio, without code."
    # The instance narrows this to the spec's asset classes (the Ranker
    # reads it off the instance).
    applicable_asset_classes: ClassVar[tuple[AssetClass, ...]] = _ALL_ASSET_CLASSES
    #: Spec used when ``params`` has none; :meth:`bind` overrides it.
    default_spec: ClassVar[Mapping[str, Any]] = SMA_TREND_FOLLOWING.spec

    @classmethod
    def parameter_spec(cls) -> ParamSpace:
        return [
            ParameterSpec(
                name="spec",
                kind="categorical",
                default=json.loads(json.dumps(cls.default_spec)),
                bounds=None,
                tunable=False,
                description="Rule spec (JSON object, version 1): indicators, entry / exit "
                "conditions, ranking, sizing and risk exits.",
            )
        ]

    def __init__(self, params: Params) -> None:
        super().__init__(params)
        self.spec: RuleSpec = validate_spec(self.params["spec"])
        self.params["spec"] = self.spec.model_dump(mode="json")
        self.applicable_asset_classes = tuple(self.spec.universe.asset_classes)  # type: ignore[misc]
        self._interval = Interval.parse(self.spec.interval)
        self._window = window_size(self.spec)
        self._tickers = set(self.spec.universe.tickers or ())
        self._bar_caches = LakeBarCaches()
        self._classes: weakref.WeakKeyDictionary[Any, dict[str, str | None]] = (
            weakref.WeakKeyDictionary()
        )
        self._evals_as_of: str | None = None
        self._evals: dict[str, _Evaluation] = {}
        self._entry_refs: dict[str, float] = {}
        #: ticker -> [high-water mark since entry, trailing stop level]
        self._trails: dict[str, list[float]] = {}
        #: ticker -> the day the entry reference and trail are priced in
        self._ref_days: dict[str, date] = {}
        #: The last lake ``estimate_return`` read (``decide`` gets none).
        self._last_lake: weakref.ref | None = None

    @classmethod
    def bind(cls, spec: Any) -> type[RuleStrategy]:
        """A subclass whose default ``spec`` is ``spec``: lets class-based
        tooling (the lab's tuner) build instances of *this* rule set. It
        saves and registers as plain :class:`RuleStrategy` with the spec in
        its params: the subclass keeps the ``RuleStrategy`` name and module,
        so even ``module:Class`` derived from ``type(instance)`` (what the
        registry stores) resolves to the plain class."""
        normalized = validate_spec(spec).model_dump(mode="json")
        return type(
            cls.__name__,
            (cls,),
            {"default_spec": normalized, "__module__": cls.__module__, "__doc__": cls.__doc__},
        )

    @classmethod
    def _class_path(cls) -> str:
        return RULE_STRATEGY_CLASS_PATH

    # ---- Strategy Protocol -------------------------------------------------

    def estimate_return(self, ticker: str, as_of: Any, lake: Any) -> float | None:
        # Forget any earlier evaluation of this ticker first, so decide()
        # never acts on one from another lake or run.
        self._remember(as_of, ticker, None)
        if lake is None or not self._in_universe(ticker, lake):
            return None
        try:
            self._last_lake = weakref.ref(lake)
        except TypeError:
            self._last_lake = None
        bars = self._bar_caches.for_lake(lake).last_n_bars(
            ticker, self._interval, as_of, self._window
        )
        width = None
        if self.spec.risk.has_trailing_stop:
            per_year = periods_per_year(self._asset_class(ticker, lake), self._interval)  # type: ignore[arg-type]
            width = stop_width(self.spec.risk, bars, per_year)
        values = snapshot(self.spec, bars)
        if values is None:
            if width is not None:
                self._remember(as_of, ticker, _Evaluation(False, False, width))
            return None
        ev = _Evaluation(
            entry=evaluate(self.spec.entry, values),
            exit=self.spec.exit is not None and evaluate(self.spec.exit, values),
            stop_width=width,
        )
        self._remember(as_of, ticker, ev)
        if not ev.entry or ev.exit:
            return None
        return _rank_score(values[self.spec.rank.by][1], self.spec.rank.order)

    def decide(
        self,
        my_picks: Sequence[tuple[float, str]],
        portfolio: Portfolio,
        prices: Mapping[str, float],
        as_of: Any,
    ) -> list[Order]:
        evals = self._evals if self._evals_as_of == self._key(as_of) else {}
        picked = {t for _, t in my_picks}
        held = {t: q for t, q in portfolio.positions.items() if q > 0}
        for refs in (self._entry_refs, self._trails, self._ref_days):
            for ticker in list(refs):
                if ticker not in held:
                    del refs[ticker]

        if self.spec.risk.has_price_exit:
            self._follow_splits(as_of)
            for ticker in held:
                if ticker not in self._entry_refs:
                    self._restore_entry(ticker, as_of)

        orders: list[Order] = []
        for ticker, qty in held.items():
            if self._should_exit(ticker, prices.get(ticker), evals.get(ticker), picked):
                orders.append(self._order("sell", ticker, qty, as_of))
                self._entry_refs.pop(ticker, None)
                self._trails.pop(ticker, None)

        sizing = self.spec.sizing
        remaining = len(held) - len(orders)
        slots = max(0, sizing.max_positions - remaining)
        if sizing.top_k is not None:
            slots = min(slots, sizing.top_k)
        equity = portfolio.cash + sum(q * float(prices.get(t) or 0.0) for t, q in held.items())
        budget = sizing.allocation * equity / sizing.max_positions
        cash = float(portfolio.cash)
        for _, ticker in sorted(my_picks, key=lambda p: p[0], reverse=True):
            if slots <= 0 or cash <= 0:
                break
            price = prices.get(ticker)
            if ticker in held or not price or price <= 0:
                continue
            spend = min(budget, cash)
            if spend <= 0:
                break
            orders.append(self._order("buy", ticker, spend / price, as_of))
            self._entry_refs[ticker] = float(price)
            self._trails[ticker] = [float(price), -math.inf]
            self._ref_days[ticker] = as_datetime(as_of).date()
            cash -= spend
            slots -= 1
        return orders

    # ---- internals ---------------------------------------------------------

    @staticmethod
    def _key(as_of: Any) -> str:
        return iso(as_datetime(as_of))

    def _remember(self, as_of: Any, ticker: str, ev: _Evaluation | None) -> None:
        key = self._key(as_of)
        if key != self._evals_as_of:
            self._evals_as_of, self._evals = key, {}
        if ev is None:
            self._evals.pop(ticker, None)
        else:
            self._evals[ticker] = ev

    def _should_exit(
        self,
        ticker: str,
        price: float | None,
        ev: _Evaluation | None,
        picked: set[str],
    ) -> bool:
        if ev is not None and ev.exit:
            return True
        if self.spec.exit_when_entry_false and ticker not in picked:
            return True
        ref = self._entry_refs.get(ticker)
        if ref is None or not price or price <= 0:
            return False
        risk = self.spec.risk
        if risk.stop_loss_pct is not None and price <= ref * (1.0 - risk.stop_loss_pct):
            return True
        if self._trailing_stop_hit(ticker, float(price), ev):
            return True
        return risk.take_profit_pct is not None and price >= ref * (1.0 + risk.take_profit_pct)

    def _follow_splits(self, as_of: Any) -> None:
        """Re-express each entry reference and trail in the shares of
        ``as_of``: ``decide`` gets raw prices, so a split since the
        reference was taken would read as a crash (or a jump)."""
        lake = self._last_lake() if self._last_lake is not None else None
        if lake is None or not self._ref_days:
            return
        today = as_datetime(as_of).date()
        actions = LakeCorporateActions(lake).load(sorted(self._ref_days))
        for ticker, since in list(self._ref_days.items()):
            factor = 1.0
            for event in actions.for_ticker(ticker):
                if isinstance(event, Split) and since < event.ex_date <= today:
                    factor *= event.ratio
            if factor != 1.0:
                if ticker in self._entry_refs:
                    self._entry_refs[ticker] /= factor
                trail = self._trails.get(ticker)
                if trail is not None:
                    self._trails[ticker] = [v / factor for v in trail]
            self._ref_days[ticker] = today

    def _trailing_stop_hit(self, ticker: str, price: float, ev: _Evaluation | None) -> bool:
        """Raise the high-water mark and the (never lowered) stop level,
        then test the close against it."""
        trail = self._trails.get(ticker)
        if trail is None:
            return False
        trail[0] = max(trail[0], price)
        if ev is not None and ev.stop_width is not None:
            trail[1] = max(trail[1], trail[0] - ev.stop_width)
        return price <= trail[1]

    def _restore_entry(self, ticker: str, as_of: Any) -> None:
        """Rebuild a held ticker's entry price, high-water mark and trailing
        stop from the bars before ``as_of`` (see the module doc)."""
        lake = self._last_lake() if self._last_lake is not None else None
        if lake is None or not self._in_universe(ticker, lake):
            return
        bars = self._bar_caches.for_lake(lake).last_n_bars(
            ticker, self._interval, as_of, REPLAY_BARS + self._window
        )
        n = len(bars)
        if n < 2:
            return
        first = max(0, n - 1 - REPLAY_BARS)

        def window(i: int) -> Any:
            return bars.iloc[max(0, i + 1 - self._window) : i + 1]

        def on(i: int) -> bool:
            values = snapshot(self.spec, window(i))
            if values is None or not evaluate(self.spec.entry, values):
                return False
            return self.spec.exit is None or not evaluate(self.spec.exit, values)

        # the latest signal run that started before this bar
        j = n - 2
        while j >= first and not on(j):
            j -= 1
        if j < first:
            return
        start = j
        while start > first and on(start - 1):
            start -= 1
        closes = bars["close"].to_numpy(dtype=float)
        self._entry_refs[ticker] = float(closes[start])
        trail = [float(closes[start]), -math.inf]
        per_year = None
        if self.spec.risk.has_trailing_stop:
            per_year = periods_per_year(self._asset_class(ticker, lake), self._interval)  # type: ignore[arg-type]
        for k in range(start + 1, n - 1):
            trail[0] = max(trail[0], float(closes[k]))
            if per_year is not None:
                width = stop_width(self.spec.risk, window(k), per_year)
                if width is not None:
                    trail[1] = max(trail[1], trail[0] - width)
        self._trails[ticker] = trail
        # the bars are adjusted as of their last one: priced in today's shares
        self._ref_days[ticker] = as_datetime(as_of).date()

    def _in_universe(self, ticker: str, lake: Any) -> bool:
        if self._tickers and ticker not in self._tickers:
            return False
        return self._asset_class(ticker, lake) in self.applicable_asset_classes

    def _asset_class(self, ticker: str, lake: Any) -> str:
        """The ticker's asset class; ``equity`` (the default class) when the
        lake has no instrument profile for it."""
        try:
            cache = self._classes.setdefault(lake, {})
        except TypeError:
            cache = {}
        if ticker not in cache:
            lookup = getattr(lake, "get_asset_classes", None)
            cache[ticker] = lookup([ticker]).get(ticker) if callable(lookup) else None
        return cache[ticker] or "equity"

    def _order(self, side: str, ticker: str, qty: float, as_of: Any) -> Order:
        return Order(
            client_id=f"{self.id}:{side}:{ticker}:{iso(as_of)}",
            ticker=ticker,
            side=side,  # type: ignore[arg-type]
            quantity=float(qty),
            order_type="market",
            strategy_id=self.id,
        )
