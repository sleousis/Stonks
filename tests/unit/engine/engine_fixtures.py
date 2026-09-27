"""Shared parts for the engine process tests (roadmap 21.2.5)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from stonks.backtest.costs import FixedCostModel
from stonks.backtest.fills import MinuteFillSettings
from stonks.core.clock import FakeClock
from stonks.core.interval import Interval
from stonks.core.stream import StreamBar
from stonks.engine.control import EngineControl
from stonks.engine.process import EngineBook, EngineProcess, RoutedDecision
from stonks.engine.sessions import SessionRules
from stonks.engine.step import StepBook
from stonks.store.state import SqliteState
from stonks.streaming.recorder import StreamRecorder
from stonks.streaming.sources.replay import ReplaySource
from tests.unit.engine.minute_lake import DAYS, TICKERS, MinuteMomentum, minute_frame

#: Fills that match the bar backtester: whole orders at the next open.
PARITY_FILLS = MinuteFillSettings(
    max_participation=None, max_gap_bars=None, expire_at_session_end=False
)


def parity_costs() -> FixedCostModel:
    return FixedCostModel(2.0, 1.0)


def add_portfolio(state: SqliteState, portfolio_id: str) -> None:
    owner = state.sql("SELECT id FROM users ORDER BY created_at LIMIT 1")[0]["id"]
    state.execute(
        "INSERT INTO portfolios (id, owner_id, name, kind, created_at)"
        " VALUES (?, ?, ?, 'simulated', '2026-01-01T00:00:00')",
        [portfolio_id, owner, portfolio_id],
    )


def record(frame: pd.DataFrame, root: Path) -> Path:
    """``frame``'s bars as a stream recording under ``root``."""
    rec = StreamRecorder(root, chunk_events=10_000_000, chunk_every=timedelta(days=365))
    for row in frame.sort_values(["timestamp", "ticker"]).itertuples(index=False):
        ts = pd.Timestamp(row.timestamp).to_pydatetime().replace(tzinfo=UTC)
        rec.write(
            StreamBar(
                row.ticker,
                ts,
                Interval.MIN_1,
                float(row.open),
                float(row.high),
                float(row.low),
                float(row.close),
                int(row.volume),
                source="recording",
            )
        )
    rec.close()
    return root


def day_frame(day: date) -> pd.DataFrame:
    frame = minute_frame()
    return frame[pd.to_datetime(frame["timestamp"]).dt.date == day]


def book(
    portfolio_id: str,
    *,
    sessions: SessionRules | None = None,
    book_id: str = "intraday",
    **kw: Any,
) -> EngineBook:
    kw.setdefault("fill", PARITY_FILLS)
    kw.setdefault("cost_model", parity_costs())
    kw.setdefault("session_key", None)
    return EngineBook(
        spec=StepBook(id=book_id, portfolio_id=portfolio_id, sessions=sessions),
        initial_cash=100_000.0,
        **kw,
    )


def replay_process(
    lake: Any,
    state: SqliteState,
    recording: Path,
    books: Sequence[EngineBook],
    *,
    session: date = DAYS[0],
    strategy: Any = None,
    control: EngineControl | None = None,
    decisions: list[RoutedDecision] | None = None,
    start: datetime | None = None,
    **kw: Any,
) -> EngineProcess:
    clock = FakeClock(start or datetime(session.year, session.month, session.day, tzinfo=UTC))
    sink = decisions if decisions is not None else []
    return EngineProcess(
        strategies={"0": strategy or MinuteMomentum({"lookback": 10})},
        books=books,
        lake=lake,
        state=state,
        universe=TICKERS,
        session=session,
        source=ReplaySource(recording),
        clock=clock,
        control=control,
        on_decision=sink.append,
        **kw,
    )
