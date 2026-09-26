"""QualityValue — a point-in-time value + quality fundamentals strategy.

For each equity it scores:

- **value**: earnings yield (TTM net income / market cap) and free-cash-flow
  yield (TTM FCF / market cap), market cap = last close x shares outstanding;
- **quality**: return on equity (TTM net income / equity), gross margin
  (TTM gross profit / revenue) and low leverage (1 - liabilities / assets).

Each metric is clipped to a plausible range and scaled to ``[-1, 1]``; the
score is their weighted mean over the metrics that could be computed (the
weights are tunable). ``estimate_return`` maps the score to an expected
return (``return_scale * score``); ``decide`` holds the top-K picks equally
weighted and exits everything else.

Point in time: a statement row is visible only from its ``filing_date``,
or ``period_end + missing_filing_lag_days`` when the filing date is unknown
(see :meth:`DuckDBLake.get_statement_history`). Flows are trailing twelve
months from the last four visible quarterlies, falling back to the latest
visible annual report when that is newer or the quarters are incomplete.
Fundamentals older than ``max_statement_age_days`` and prices older than a
few days (a delisted or halted name) produce no signal.
"""

from __future__ import annotations

import math
import weakref
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

import numpy as np
import pandas as pd

from stonks.core.interval import Interval
from stonks.core.params import ParameterSpec
from stonks.core.types import Features, Order, Portfolio
from stonks.features.library import ttm_from_quarters
from stonks.strategies._common import LakeBarCaches, as_datetime, iso
from stonks.strategies.base import BaseStrategy

# A last close older than this (calendar days) means the name stopped
# trading (delisted, halted); its frozen price must not look like value.
_MAX_PRICE_STALENESS_DAYS = 10

_INCOME_FLOWS = ("revenue", "cost_of_revenue", "gross_profit", "net_income")
_CASH_FLOWS = ("free_cash_flow", "operating_cash_flow", "capital_expenditures")
_BALANCE_ITEMS = (
    "total_stockholder_equity",
    "total_assets",
    "total_liabilities",
    "common_stock_shares_outstanding",
)

# metric -> (weight param, lower clip, upper clip); scaled to [-1, 1].
_METRICS: dict[str, tuple[str, float, float]] = {
    "earnings_yield": ("w_earnings_yield", -0.25, 0.25),
    "fcf_yield": ("w_fcf_yield", -0.25, 0.25),
    "roe": ("w_roe", -0.5, 0.5),
    "gross_margin": ("w_gross_margin", -1.0, 1.0),
    "low_leverage": ("w_low_leverage", -1.0, 1.0),
}
_GROUPS = (("earnings_yield", "fcf_yield"), ("roe", "gross_margin", "low_leverage"))


@dataclass(frozen=True)
class _Snapshot:
    """Values from one statement as visible at some date, plus the period
    end they describe (for staleness checks)."""

    values: Mapping[str, float | None]
    period_end: date


class _History:
    """One ticker's rows of one statement table, oldest period first."""

    def __init__(self, df: pd.DataFrame) -> None:
        self.frame = df.reset_index(drop=True)
        self.available = np.array(
            [np.datetime64(d, "D") for d in df.get("available_date", [])], dtype="datetime64[D]"
        )
        self.period_end = list(df.get("period_end", []))
        self.frequency = np.asarray(df.get("frequency", []), dtype=object)
        self.memo: dict[bytes, Any] = {}

    def visible(self, as_of: date) -> np.ndarray:
        return self.available <= np.datetime64(as_of, "D")

    def column(self, name: str) -> list[float | None]:
        if name not in self.frame.columns:
            return [None] * len(self.frame)
        return [_finite(v) for v in self.frame[name]]


def _finite(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _ratio(num: float | None, den: float | None) -> float | None:
    """``num / den`` for a strictly positive denominator, else ``None``."""
    if num is None or den is None or den <= 0:
        return None
    return num / den


class QualityValue(BaseStrategy):
    id = "quality_value"
    applicable_asset_classes = ("equity",)

    def __init__(self, params: Any) -> None:
        super().__init__(params)
        self._bar_caches = LakeBarCaches()
        # lake -> {(table, ticker): _History}; weak so throwaway lakes
        # (permutation / perturbation copies) don't share or leak entries.
        self._histories: weakref.WeakKeyDictionary[Any, dict[tuple[str, str], Any]]
        self._histories = weakref.WeakKeyDictionary()

    @classmethod
    def parameter_spec(cls):
        def weight(name: str, default: float, what: str) -> ParameterSpec:
            return ParameterSpec(
                name=name,
                kind="float",
                default=default,
                bounds=(0.0, 5.0),
                description=f"Weight of {what} in the composite score.",
            )

        return [
            weight("w_earnings_yield", 1.0, "earnings yield (TTM net income / market cap)"),
            weight("w_fcf_yield", 1.0, "free-cash-flow yield (TTM FCF / market cap)"),
            weight("w_roe", 1.0, "return on equity (TTM net income / equity)"),
            weight("w_gross_margin", 0.5, "gross margin (TTM gross profit / revenue)"),
            weight("w_low_leverage", 0.5, "low leverage (1 - liabilities / assets)"),
            ParameterSpec(
                name="top_k",
                kind="int",
                default=10,
                bounds=(1, 100),
                description="Number of top-scored names held, equally weighted.",
            ),
            ParameterSpec(
                name="min_score",
                kind="float",
                default=0.0,
                bounds=(-1.0, 1.0),
                description="Composite score (in [-1, 1]) a name must exceed to be a pick.",
            ),
            ParameterSpec(
                name="return_scale",
                kind="float",
                default=0.1,
                bounds=(0.0, 1.0),
                tunable=False,
                description="Expected return reported for a score of 1.",
            ),
            ParameterSpec(
                name="missing_filing_lag_days",
                kind="int",
                default=90,
                bounds=(0, 365),
                tunable=False,
                description="Days after period end a statement with no filing date "
                "is treated as public. Also the publication lag on share counts.",
            ),
            ParameterSpec(
                name="max_statement_age_days",
                kind="int",
                default=550,
                bounds=(90, 1500),
                tunable=False,
                description="Ignore fundamentals whose period ended longer ago than this.",
            ),
            ParameterSpec(
                name="allocation",
                kind="float",
                default=1.0,
                bounds=(0.0, 1.0),
                tunable=False,
                description="Fraction of portfolio equity spread across the top-K slots.",
            ),
        ]

    # ---- Strategy Protocol -------------------------------------------------

    def extract_features(self, ticker: str, as_of: Any, lake: Any) -> Features:
        if lake is None:
            return Features(values={})
        metrics = self._metrics(ticker, as_of, lake)
        score = self._score(metrics)
        values = {k: v for k, v in metrics.items() if v is not None}
        if score is not None:
            values["score"] = score
        return Features(values=values)

    def estimate_return(self, ticker: str, as_of: Any, lake: Any) -> float | None:
        if lake is None:
            return None
        metrics = self._metrics(ticker, as_of, lake)
        if metrics.get("market_cap") is None:
            return None
        score = self._score(metrics)
        if score is None or score <= float(self.params["min_score"]):
            return None
        expected = float(self.params["return_scale"]) * score
        return expected if expected > 0 else None

    def decide(
        self,
        my_picks: Sequence[tuple[float, str]],
        portfolio: Portfolio,
        prices: Mapping[str, float],
        as_of: Any,
    ) -> list[Order]:
        top_k = int(self.params["top_k"])
        ranked = sorted(my_picks, key=lambda p: p[0], reverse=True)
        targets = [t for _, t in ranked if (prices.get(t) or 0.0) > 0][:top_k]
        target_set = set(targets)

        orders: list[Order] = []
        budget = max(portfolio.cash, 0.0)
        equity = portfolio.cash + sum(
            qty * prices.get(t, 0.0) for t, qty in portfolio.positions.items()
        )

        # Sells first: the engine fills in list order, so their proceeds fund the buys.
        for ticker, qty in portfolio.positions.items():
            if qty > 0 and ticker not in target_set:
                orders.append(self._order("sell", ticker, qty, as_of))
                budget += qty * prices.get(ticker, 0.0)

        slot = float(self.params["allocation"]) * max(equity, 0.0) / top_k
        for ticker in targets:
            if portfolio.positions.get(ticker, 0.0) > 0:
                continue
            notional = min(slot, budget)
            if notional <= 0:
                break
            orders.append(self._order("buy", ticker, notional / prices[ticker], as_of))
            budget -= notional
        return orders

    # ---- internals ---------------------------------------------------------

    def _order(self, side: str, ticker: str, qty: float, as_of: Any) -> Order:
        return Order(
            client_id=f"{self.id}:{side}:{ticker}:{iso(as_of)}",
            ticker=ticker,
            side=side,  # type: ignore[arg-type]
            quantity=qty,
            order_type="market",
            strategy_id=self.id,
        )

    def _score(self, metrics: Mapping[str, float | None]) -> float | None:
        """Weighted mean of the scaled metrics, or ``None`` unless every
        group (value, quality) that carries weight has at least one metric:
        a clean balance sheet alone is not a value + quality signal."""
        total = weights = 0.0
        for group in _GROUPS:
            group_weight = seen = 0.0
            for name in group:
                param, lo, hi = _METRICS[name]
                w = float(self.params[param])
                group_weight += max(w, 0.0)
                value = metrics.get(name)
                if value is None or w <= 0:
                    continue
                scaled = 2.0 * (min(max(value, lo), hi) - lo) / (hi - lo) - 1.0
                total += w * scaled
                weights += w
                seen += w
            if group_weight > 0 and seen == 0:
                return None
        return total / weights if weights > 0 else None

    def _metrics(self, ticker: str, as_of: Any, lake: Any) -> dict[str, float | None]:
        day = as_datetime(as_of).date()
        oldest = day - timedelta(days=int(self.params["max_statement_age_days"]))

        def fresh(snap: _Snapshot | None) -> Mapping[str, float | None]:
            return snap.values if snap is not None and snap.period_end >= oldest else {}

        income = fresh(self._flows(lake, "income_statement", ticker, day, _INCOME_FLOWS))
        cash = fresh(self._flows(lake, "cash_flow_statement", ticker, day, _CASH_FLOWS))
        balance = fresh(self._balance(lake, ticker, day))
        if not (income or cash or balance):
            return {}

        net_income = income.get("net_income")
        revenue = income.get("revenue")
        gross_profit = income.get("gross_profit")
        if gross_profit is None and revenue is not None and income.get("cost_of_revenue"):
            gross_profit = revenue - abs(income["cost_of_revenue"])
        fcf = cash.get("free_cash_flow")
        if fcf is None and cash.get("operating_cash_flow") is not None:
            capex = cash.get("capital_expenditures")
            if capex is not None:
                fcf = cash["operating_cash_flow"] - abs(capex)

        market_cap = None
        price = self._price(lake, ticker, as_of, day)
        shares = self._shares(lake, ticker, day, balance)
        if price is not None and shares is not None:
            market_cap = price * shares

        leverage = _ratio(balance.get("total_liabilities"), balance.get("total_assets"))
        return {
            "market_cap": market_cap,
            "earnings_yield": _ratio(net_income, market_cap),
            "fcf_yield": _ratio(fcf, market_cap),
            "roe": _ratio(net_income, balance.get("total_stockholder_equity")),
            "gross_margin": _ratio(gross_profit, revenue),
            "leverage": leverage,
            "low_leverage": None if leverage is None else 1.0 - 2.0 * min(leverage, 1.0),
        }

    def _history(self, lake: Any, table: str, ticker: str) -> _History:
        try:
            per_lake = self._histories.setdefault(lake, {})
        except TypeError:  # lake can't be weakly referenced; skip the cache
            per_lake = {}
        key = (table, ticker)
        hist = per_lake.get(key)
        if hist is None:
            df = lake.get_statement_history(
                table, ticker, missing_filing_lag_days=int(self.params["missing_filing_lag_days"])
            )
            hist = _History(df)
            per_lake[key] = hist
        return hist

    def _flows(
        self, lake: Any, table: str, ticker: str, day: date, cols: tuple[str, ...]
    ) -> _Snapshot | None:
        hist = self._history(lake, table, ticker)
        visible = hist.visible(day)
        key = visible.tobytes()
        if key not in hist.memo:
            hist.memo[key] = _trailing_flows(hist, visible, cols)
        return hist.memo[key]

    def _balance(self, lake: Any, ticker: str, day: date) -> _Snapshot | None:
        hist = self._history(lake, "balance_sheet", ticker)
        visible = hist.visible(day)
        key = visible.tobytes()
        if key not in hist.memo:
            idx = np.flatnonzero(visible)
            snap = None
            if len(idx):
                i = int(idx[-1])  # rows are ordered by period end
                snap = _Snapshot(
                    values={c: hist.column(c)[i] for c in _BALANCE_ITEMS},
                    period_end=hist.period_end[i],
                )
            hist.memo[key] = snap
        return hist.memo[key]

    def _price(self, lake: Any, ticker: str, as_of: Any, day: date) -> float | None:
        cutoff = datetime.combine(day, time.max)
        last = self._bar_caches.for_lake(lake).last_close(ticker, Interval.DAY_1, cutoff)
        if last is None:
            return None
        ts, close = last
        if (day - ts.date()).days > _MAX_PRICE_STALENESS_DAYS or not close > 0:
            return None
        return close

    def _shares(
        self, lake: Any, ticker: str, day: date, balance: Mapping[str, float | None]
    ) -> float | None:
        shares = balance.get("common_stock_shares_outstanding")
        if shares is not None and shares > 0:
            return shares
        # Fallback: the share-count table, lagged like an unfiled statement.
        lag = timedelta(days=int(self.params["missing_filing_lag_days"]))
        hist = self._history_shares(lake, ticker)
        for d, s in reversed(hist):
            if d + lag <= day:
                return s if s is not None and s > 0 else None
        return None

    def _history_shares(self, lake: Any, ticker: str) -> list[tuple[date, float | None]]:
        try:
            per_lake = self._histories.setdefault(lake, {})
        except TypeError:
            per_lake = {}
        key = ("shares_outstanding", ticker)
        if key not in per_lake:
            df = lake.get_shares_outstanding(ticker)
            per_lake[key] = [(d, _finite(s)) for d, s in zip(df["date"], df["shares"], strict=True)]
        return per_lake[key]


def _trailing_flows(hist: _History, visible: np.ndarray, cols: tuple[str, ...]) -> _Snapshot | None:
    """TTM from the last four visible quarterlies, or the latest visible
    annual report when it is newer or the quarters don't add up to a TTM."""
    quarters = np.flatnonzero(visible & (hist.frequency == "Q"))
    annuals = np.flatnonzero(visible & (hist.frequency == "A"))

    ttm: _Snapshot | None = None
    if len(quarters) >= 4:
        last4 = quarters[-4:]
        ends = [hist.period_end[i] for i in last4]
        values = {c: ttm_from_quarters(ends, [hist.column(c)[i] for i in last4]) for c in cols}
        if any(v is not None for v in values.values()):
            ttm = _Snapshot(values=values, period_end=ends[-1])

    annual: _Snapshot | None = None
    if len(annuals):
        i = int(annuals[-1])
        annual = _Snapshot(
            values={c: hist.column(c)[i] for c in cols}, period_end=hist.period_end[i]
        )

    if ttm is not None and (annual is None or ttm.period_end >= annual.period_end):
        return ttm
    return annual
