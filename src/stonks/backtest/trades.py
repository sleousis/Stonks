"""Round-trip trade ledger and trade statistics (BL-02).

Pure functions over a backtest's ``Fill`` sequence: every caller (lab, API
backtests, reporting, survival tests) reads ``BacktestReport.trades`` and
``BacktestReport.trade_stats`` instead of pairing fills itself.

Pairing (FIFO, long and short lots)
-----------------------------------
- A backtest trades one portfolio, so lots are pooled per ticker (RS-04).
  Every lot is labelled with its strategy key: the ``"<index>:"`` prefix
  the engine puts on each ``client_id`` (``""`` when there is none).
- A fill first closes the open lots of the other direction: a sell closes
  long lots, a buy closes short lots (a cover). It takes the oldest lots
  with its own strategy key first, then the oldest lots of any other key.
  So two strategy instances keep their own round trips while each closes
  what it opened, and a fill whose key owns no lot (a construction
  pipeline whose owner changed, a risk-rule exit whose client id starts
  with a date) still closes the shares the portfolio holds. The key is a
  label for attribution, never a reason to leave a lot open.
- What is left of the fill opens a lot in its own direction: a buy opens a
  long lot, a sell beyond the long lots opens a short lot (roadmap 16.1).
  Sell-open then buy-close is one short round trip (``side = "short"``).
- Each (lot, closing fill) pair is one ``RoundTrip``, so a partial exit
  splits the lot and one fill can close several lots.
- Lots still open at the end are marked at the last close (``bars``), else
  at ``marks``, else at the ticker's last fill price, and flagged
  ``is_open``.
- Corporate actions (``BacktestReport.corporate_actions``) apply to every
  open lot of the ticker, before fills stamped at the same bar, as the
  engine does: a split multiplies the lot's quantity by its ratio and
  divides its entry price, so ``qty``/``entry_px`` are in shares at exit; a
  cash dividend credits ``cash_delta / quantity_before`` per held share to
  the lot, and is part of its P&L.

Money
-----
- ``pnl = (exit_px - entry_px) * qty - fees + dividends`` for a long lot and
  ``(entry_px - exit_px) * qty - fees + dividends`` for a short one, where
  ``fees`` is the lot's share of its opening fee plus its share of the
  closing fee (both pro rata to quantity). A short's dividends are the
  (negative) dividends it paid. Fill prices already include slippage, so
  ``slippage_cost = |fill price - reference price| * qty`` (entry and exit)
  is reported, not subtracted again.
- ``return_pct = pnl / (entry_px * qty + entry fee)``, the cost basis.
- ``bars_held`` counts bars of the backtest timeline (its equity marks)
  closed with the lot held: ``exit index - entry index`` for a closed lot
  (fills happen at a bar's open), up to and including the last bar for an
  open one.
- MAE / MFE are the worst and best excursion from the entry price over
  [entry bar, exit bar] (split-adjusted, bounded by 0): the bars' low and
  high for a long, their high and low for a short (a rise hurts it). ``None``
  when no bars are passed.
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Literal

import numpy as np
import pandas as pd

from stonks.backtest import metrics
from stonks.backtest.corporate_actions import CorporateActionRecord
from stonks.core.types import Fill

if TYPE_CHECKING:
    from stonks.backtest.report import BacktestReport

#: Relative size below which a lot remainder is float dust, not a position.
_DUST = 1e-9
ReferencePrice = Callable[[str], float | None]
TradeSide = Literal["long", "short"]


@dataclass(frozen=True)
class RoundTrip:
    """One lot (or part of one) from its buy to its sell, or to the end."""

    ticker: str
    #: The engine's ``"<index>"`` strategy prefix of the client ids.
    strategy_key: str
    entry_ts: datetime
    exit_ts: datetime
    #: Shares, in post-split terms at exit.
    qty: float
    entry_px: float
    exit_px: float
    pnl: float
    return_pct: float
    bars_held: int
    fees: float
    slippage_cost: float
    mae_pct: float | None
    mfe_pct: float | None
    is_open: bool
    #: Cash dividends credited while the lot was held (inside ``pnl``).
    dividends: float = 0.0
    #: ``long`` (bought then sold) or ``short`` (sold then bought back).
    side: TradeSide = "long"


@dataclass(frozen=True)
class TradeStats:
    """Trade-level statistics. Win/loss figures are over closed trades;
    ``exposure``, turnover and costs cover every fill and lot."""

    n_trades: int = 0
    n_open: int = 0
    win_rate: float = 0.0
    avg_win: float = 0.0
    #: Mean P&L of the non-winning trades (<= 0).
    avg_loss: float = 0.0
    payoff_ratio: float = 0.0
    #: Mean P&L per closed trade: ``p * avg_win + (1 - p) * avg_loss``.
    expectancy: float = 0.0
    trade_profit_factor: float = 0.0
    avg_bars_held: float = 0.0
    #: Share of the timeline's bars that closed with any position held.
    exposure: float = 0.0
    #: ``sum |traded notional| / mean equity / years``.
    turnover_annual: float = 0.0
    #: ``sum (fees + |fill - reference price| * qty)`` over every fill.
    costs_paid: float = 0.0
    #: ``costs_paid / mean equity / years``.
    cost_drag_annual: float = 0.0


# ---- ledger ---------------------------------------------------------------------


@dataclass
class _Lot:
    strategy_key: str
    ticker: str
    entry_ts: datetime
    qty: float
    #: ``qty`` at entry, rescaled by splits; the dust threshold's base.
    size: float
    entry_px: float
    fee: float
    slippage: float
    dividends: float = 0.0
    #: ``1`` for a long lot, ``-1`` for a short one.
    direction: int = 1

    def take(self, qty: float) -> tuple[float, float, float]:
        """Remove ``qty`` shares; return their share of the entry fee,
        entry slippage and dividends."""
        frac = qty / self.qty
        parts = (self.fee * frac, self.slippage * frac, self.dividends * frac)
        self.qty -= qty
        self.fee -= parts[0]
        self.slippage -= parts[1]
        self.dividends -= parts[2]
        return parts


def build_round_trips(
    fills: Iterable[Fill],
    *,
    timeline: Sequence[date] = (),
    corporate_actions: Iterable[CorporateActionRecord] = (),
    bars: pd.DataFrame | None = None,
    marks: Mapping[str, float] | None = None,
    reference_price: ReferencePrice | None = None,
) -> list[RoundTrip]:
    """Round trips from ``fills`` (see the module docstring for the rules).

    ``timeline`` is the backtest's bar timestamps (``equity_dates``);
    ``bars`` has columns ``ticker, timestamp, high, low, close`` and feeds
    MAE/MFE and the open-lot mark. Trades come out closed ones first, by
    exit, then open lots by entry."""
    clock = [_utc(t) for t in timeline]
    excursions = _Excursions(bars)
    splits: dict[str, list[tuple[datetime, float]]] = {}
    events: list[tuple[datetime, int, Fill | CorporateActionRecord]] = []
    for record in corporate_actions:
        events.append((_utc(record.timestamp), 0, record))
        if record.kind == "split":
            splits.setdefault(record.ticker, []).append((_utc(record.timestamp), record.value))
    events.extend((_utc(f.filled_at), 1, f) for f in fills if f.quantity > 0)
    events.sort(key=lambda e: (e[0], e[1]))  # stable: fill order kept

    def excursion(lot: _Lot, exit_: datetime):
        mae, mfe = excursions.mae_mfe(
            lot.ticker,
            lot.entry_ts,
            exit_,
            lot.entry_px,
            splits.get(lot.ticker, []),
            direction=lot.direction,
        )
        return mae, mfe

    def bars_between(entry: datetime, exit_: datetime | None) -> int:
        start = bisect.bisect_left(clock, entry)
        end = len(clock) if exit_ is None else bisect.bisect_left(clock, exit_)
        return max(end - start, 0)

    lots: dict[str, list[_Lot]] = {}
    last_price: dict[str, float] = {}
    trades: list[RoundTrip] = []
    for ts, _, event in events:
        if isinstance(event, CorporateActionRecord):
            _apply_corporate_action(lots, event)
            continue
        fill = event
        last_price[fill.ticker] = fill.price
        key = _strategy_key(fill.order_client_id)
        slip = _slippage(fill, reference_price)
        queue = lots.setdefault(fill.ticker, [])
        direction = 1 if fill.side == "buy" else -1
        to_close = fill.quantity
        for lot in _close_order(queue, key, -direction):
            if to_close <= _DUST * fill.quantity:
                break
            qty = min(lot.qty, to_close)
            share = qty / fill.quantity
            trades.append(
                _trip(
                    lot,
                    qty,
                    exit_ts=ts,
                    exit_px=fill.price,
                    exit_fee=fill.fee * share,
                    exit_slippage=slip * share,
                    bars_held=bars_between(lot.entry_ts, ts),
                    excursion=excursion,
                    is_open=False,
                )
            )
            to_close -= qty
        queue[:] = [lot for lot in queue if lot.qty > _DUST * lot.size]
        if to_close > _DUST * fill.quantity:
            share = to_close / fill.quantity
            queue.append(
                _Lot(
                    key,
                    fill.ticker,
                    ts,
                    to_close,
                    to_close,
                    fill.price,
                    fill.fee * share,
                    slip * share,
                    direction=direction,
                )
            )

    end = clock[-1] if clock else None
    open_trips = []
    for queue in lots.values():
        for lot in queue:
            exit_ts = end or excursions.last_ts(lot.ticker) or lot.entry_ts
            mark = _mark(lot.ticker, marks, excursions, last_price)
            open_trips.append(
                _trip(
                    lot,
                    lot.qty,
                    exit_ts=exit_ts,
                    exit_px=mark,
                    exit_fee=0.0,
                    exit_slippage=0.0,
                    bars_held=bars_between(lot.entry_ts, None),
                    excursion=excursion,
                    is_open=True,
                )
            )
    open_trips.sort(key=lambda t: t.entry_ts)
    return trades + open_trips


def _trip(
    lot: _Lot,
    qty: float,
    *,
    exit_ts: datetime,
    exit_px: float,
    exit_fee: float,
    exit_slippage: float,
    bars_held: int,
    excursion: Callable[[_Lot, datetime], tuple[float | None, float | None]],
    is_open: bool,
) -> RoundTrip:
    entry_px = lot.entry_px
    mae, mfe = excursion(lot, exit_ts)
    entry_fee, entry_slippage, dividends = lot.take(qty)
    fees = entry_fee + exit_fee
    pnl = lot.direction * (exit_px - entry_px) * qty - fees + dividends
    basis = entry_px * qty + entry_fee
    return RoundTrip(
        ticker=lot.ticker,
        strategy_key=lot.strategy_key,
        entry_ts=lot.entry_ts,
        exit_ts=exit_ts,
        qty=qty,
        entry_px=entry_px,
        exit_px=exit_px,
        pnl=pnl,
        return_pct=pnl / basis if basis > 0 else 0.0,
        bars_held=bars_held,
        fees=fees,
        slippage_cost=entry_slippage + exit_slippage,
        mae_pct=mae,
        mfe_pct=mfe,
        is_open=is_open,
        dividends=dividends,
        side="long" if lot.direction > 0 else "short",
    )


def _close_order(queue: Sequence[_Lot], key: str, direction: int) -> list[_Lot]:
    """The lots of ``direction`` a fill under ``key`` closes, in order: its
    own lots oldest first, then every other lot oldest first."""
    same = [lot for lot in queue if lot.direction == direction]
    return [lot for lot in same if lot.strategy_key == key] + [
        lot for lot in same if lot.strategy_key != key
    ]


def _apply_corporate_action(lots: Mapping[str, list[_Lot]], record: CorporateActionRecord) -> None:
    held = list(lots.get(record.ticker, ()))
    if record.kind == "split":
        for lot in held:
            lot.qty *= record.value
            lot.size *= record.value
            lot.entry_px /= record.value
    elif record.quantity_before:
        per_share = record.cash_delta / record.quantity_before
        for lot in held:
            lot.dividends += lot.direction * lot.qty * per_share


def _strategy_key(client_id: str) -> str:
    prefix, sep, _ = client_id.partition(":")
    return prefix if sep else ""


def _slippage(fill: Fill, reference_price: ReferencePrice | None) -> float:
    ref = reference_price(fill.order_client_id) if reference_price is not None else None
    return 0.0 if ref is None else abs(fill.price - ref) * fill.quantity


def _mark(
    ticker: str,
    marks: Mapping[str, float] | None,
    excursions: _Excursions,
    last_price: Mapping[str, float],
) -> float:
    if marks and ticker in marks:
        return marks[ticker]
    close = excursions.last_close(ticker)
    return close if close is not None else last_price[ticker]


def _utc(value: date) -> datetime:
    """Naive timestamps are UTC, as ``SimulatedBroker`` stamps fills."""
    if not isinstance(value, datetime):
        value = datetime(value.year, value.month, value.day)
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class _Excursions:
    """Per-ticker bar arrays for MAE/MFE and the last close."""

    def __init__(self, bars: pd.DataFrame | None) -> None:
        self._by_ticker: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = {}
        if bars is None or bars.empty:
            return
        frame = bars.assign(timestamp=pd.to_datetime(bars["timestamp"], utc=True)).sort_values(
            ["ticker", "timestamp"]
        )
        for ticker, group in frame.groupby("ticker", sort=False):
            close = group["close"].to_numpy(dtype=float)
            high = group["high"].to_numpy(dtype=float)
            low = group["low"].to_numpy(dtype=float)
            self._by_ticker[str(ticker)] = (
                pd.DatetimeIndex(group["timestamp"]).as_unit("ns").asi8,
                np.where(np.isnan(high), close, high),
                np.where(np.isnan(low), close, low),
                close,
            )

    def last_close(self, ticker: str) -> float | None:
        data = self._by_ticker.get(ticker)
        return None if data is None or not len(data[3]) else float(data[3][-1])

    def last_ts(self, ticker: str) -> datetime | None:
        data = self._by_ticker.get(ticker)
        if data is None or not len(data[0]):
            return None
        return pd.Timestamp(data[0][-1], tz=UTC).to_pydatetime()

    def mae_mfe(
        self,
        ticker: str,
        entry: datetime,
        exit_: datetime,
        entry_px: float,
        splits: Sequence[tuple[datetime, float]],
        *,
        direction: int = 1,
    ) -> tuple[float | None, float | None]:
        """Excursions over [entry, exit], in the lot's post-split shares:
        bars before a split inside the holding period are divided by its
        ratio. A short (``direction=-1``) loses on the high and gains on
        the low."""
        data = self._by_ticker.get(ticker)
        if data is None or entry_px <= 0:
            return None, None
        ts, high, low, _ = data
        lo = np.searchsorted(ts, _ns(entry), side="left")
        hi = np.searchsorted(ts, _ns(exit_), side="right")
        if hi <= lo:
            return None, None
        window = ts[lo:hi]
        factor = np.ones(hi - lo)
        for split_ts, ratio in splits:
            if entry < split_ts <= exit_:
                factor[window < _ns(split_ts)] /= ratio
        lowest = float((low[lo:hi] * factor).min()) / entry_px
        highest = float((high[lo:hi] * factor).max()) / entry_px
        if direction < 0:
            return min(0.0, 1.0 - highest), max(0.0, 1.0 - lowest)
        return min(0.0, lowest - 1.0), max(0.0, highest - 1.0)


def _ns(ts: datetime) -> int:
    return pd.Timestamp(ts).as_unit("ns").value


# ---- statistics -----------------------------------------------------------------


def compute_trade_stats(
    trades: Sequence[RoundTrip],
    fills: Iterable[Fill] = (),
    *,
    equity_dates: Sequence[date] = (),
    equity_curve: Sequence[float] = (),
    reference_price: ReferencePrice | None = None,
) -> TradeStats:
    """Trade statistics; turnover and cost figures need ``fills`` and the
    equity curve, ``exposure`` needs ``equity_dates``."""
    closed = [t for t in trades if not t.is_open]
    n = len(closed)
    pnls = [t.pnl for t in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0

    fills = list(fills)
    notional = sum(abs(f.quantity * f.price) for f in fills)
    costs = sum(f.fee + _slippage(f, reference_price) for f in fills)
    mean_equity = sum(equity_curve) / len(equity_curve) if equity_curve else 0.0
    per_year = mean_equity * metrics.years_spanned(list(equity_dates)) if fills else 0.0

    return TradeStats(
        n_trades=n,
        n_open=len(trades) - n,
        win_rate=len(wins) / n if n else 0.0,
        avg_win=avg_win,
        avg_loss=avg_loss,
        payoff_ratio=(avg_win / abs(avg_loss) if avg_loss else math.inf) if wins else 0.0,
        expectancy=sum(pnls) / n if n else 0.0,
        trade_profit_factor=metrics.profit_factor(pnls),
        avg_bars_held=sum(t.bars_held for t in closed) / n if n else 0.0,
        exposure=_exposure(trades, equity_dates),
        turnover_annual=notional / per_year if per_year > 0 else 0.0,
        costs_paid=costs,
        cost_drag_annual=costs / per_year if per_year > 0 else 0.0,
    )


def _exposure(trades: Sequence[RoundTrip], equity_dates: Sequence[date]) -> float:
    clock = [_utc(t) for t in equity_dates]
    if not clock:
        return 0.0
    held = np.zeros(len(clock), dtype=bool)
    for t in trades:
        start = bisect.bisect_left(clock, _utc(t.entry_ts))
        end = len(clock) if t.is_open else bisect.bisect_left(clock, _utc(t.exit_ts))
        held[start:end] = True
    return float(held.mean())


def with_trades(
    report: BacktestReport,
    fills: Iterable[Fill],
    bars: pd.DataFrame | None = None,
    *,
    reference_price: ReferencePrice | None = None,
    marks: Mapping[str, float] | None = None,
) -> BacktestReport:
    """``report`` with its trade ledger, trade statistics and fitness."""
    fills = list(fills)
    trades = build_round_trips(
        fills,
        timeline=report.equity_dates,
        corporate_actions=report.corporate_actions,
        bars=bars,
        marks=marks,
        reference_price=reference_price,
    )
    stats = compute_trade_stats(
        trades,
        fills,
        equity_dates=report.equity_dates,
        equity_curve=report.equity_curve,
        reference_price=reference_price,
    )
    # Tulchinsky's daily turnover, on the curve's own calendar (RS-32).
    turnover_daily = stats.turnover_annual / report.sessions_per_year
    return replace(
        report,
        trades=tuple(trades),
        trade_stats=stats,
        fitness=metrics.fitness(report.sharpe, report.cagr, turnover_daily),
    )
