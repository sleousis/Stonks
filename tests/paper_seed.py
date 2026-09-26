"""Seed helpers for tests that read a paper-trading period back out of
``SqliteState`` (go-live gate, HTML report)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from stonks.core.protocols import SurvivalReport
from stonks.registry.store import StrategyRegistry
from stonks.store.state import SqliteState
from stonks.strategies.examples.buy_and_hold import BuyAndHold

TICK_ID = "tick-seed"


def make_env(tmp_path: Path) -> tuple[SqliteState, StrategyRegistry]:
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    state.execute(
        "INSERT INTO tick_runs (id, started_at, finished_at, status) VALUES (?, ?, ?, 'ok')",
        [TICK_ID, "2026-01-01T00:00:00+00:00", "2026-01-01T00:01:00+00:00"],
    )
    return state, StrategyRegistry(state=state, artifacts_dir=tmp_path / "artifacts")


def register(
    registry: StrategyRegistry,
    status: str,
    reports: Sequence[SurvivalReport] = (),
    strategy_id: str | None = None,
    params: dict | None = None,
) -> str:
    strategy = BuyAndHold(params or {"ticker": "UP.US", "allocation": 1.0})
    sid = registry.register(strategy, reports=list(reports), strategy_id=strategy_id)
    registry.set_status(sid, status)
    return sid


def oos(cagr: float, passed: bool = True) -> SurvivalReport:
    return SurvivalReport(
        test_id="oos",
        passed=passed,
        metrics={"cagr_oos": cagr, "sharpe_oos": 1.0, "max_drawdown_oos": -0.1},
    )


def seed_shadow(
    state: SqliteState, strategy_id: str, points: Sequence[tuple[date, float]], fills: int = 0
) -> None:
    for day, value in points:
        state.execute(
            "INSERT INTO shadow_portfolio_snapshots (tick_id, strategy_id, as_of, taken_at, "
            "cash, positions_json, total_value) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [TICK_ID, strategy_id, day.isoformat(), f"{day}T21:00:00+00:00", value, "{}", value],
        )
    first = points[0][0] if points else date(2026, 1, 1)
    for i in range(fills):
        state.execute(
            "INSERT INTO shadow_decisions (tick_id, strategy_id, as_of, ticker, side, quantity, "
            "price, status, created_at) VALUES (?, ?, ?, ?, 'buy', 1, 10, 'filled', ?)",
            [TICK_ID, strategy_id, first.isoformat(), f"T{i}.US", f"{first}T21:00:00+00:00"],
        )


def seed_portfolio(
    state: SqliteState,
    points: Sequence[tuple[date, float]],
    positions: dict[str, float] | None = None,
) -> None:
    # Include ``as_of`` when the schema has it, so these helpers work before
    # and after snapshots gained an explicit trading date.
    cols = {r["name"] for r in state.sql("PRAGMA table_info(portfolio_snapshots)")}
    for day, value in points:
        row = {
            "tick_id": TICK_ID,
            "taken_at": f"{day}T21:00:00+00:00",
            "cash": value,
            "positions_json": json.dumps(positions or {}),
            "total_value": value,
        }
        if "as_of" in cols:
            row["as_of"] = day.isoformat()
        state.execute(
            f"INSERT INTO portfolio_snapshots ({', '.join(row)}) "
            f"VALUES ({', '.join('?' for _ in row)})",
            list(row.values()),
        )


def seed_fills(
    state: SqliteState, strategy_id: str, n: int, day: date = date(2026, 1, 2), ticker="UP.US"
) -> None:
    for i in range(n):
        cid = f"{strategy_id}-{day}-{i}"
        ts = f"{day}T21:00:00+00:00"
        state.execute(
            "INSERT INTO orders (client_id, tick_id, strategy_id, ticker, side, quantity, "
            "order_type, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'buy', 1, 'market', 'filled', ?, ?)",
            [cid, TICK_ID, strategy_id, ticker, ts, ts],
        )
        state.execute(
            "INSERT INTO fills (order_client_id, ticker, quantity, price, fee, filled_at) "
            "VALUES (?, ?, 1, 100, 0, ?)",
            [cid, ticker, ts],
        )


def days(n: int, start: date = date(2026, 1, 1)) -> list[date]:
    from datetime import timedelta

    return [start + timedelta(days=i) for i in range(n)]
