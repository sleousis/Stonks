"""Which execution algo a portfolio's orders use (roadmap 23.16).

One row per portfolio (``strategy_id = ''``) and optionally one per
strategy in it, in ``execution_algo_settings`` (SQLite 054). A strategy's
row wins over the portfolio's. No row means plain orders, the default.

:func:`attach_algos` puts the resolved algo on each order that has none
when a ticket is written, so the person approving a ticket sees how it
will be worked. Stops, option orders and orders in a one-cancels-other
group always stay plain."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from stonks.core.options import is_option_id
from stonks.core.types import Order
from stonks.execution.algos.base import algo_spec
from stonks.store.state import SqliteState

#: The ``strategy_id`` of a portfolio's own row.
PORTFOLIO_ROW = ""


@dataclass(frozen=True)
class AlgoSetting:
    portfolio_id: str
    #: ``None``: the portfolio's own setting.
    strategy_id: str | None
    algo: str
    params: Mapping[str, Any]
    updated_by: str | None
    updated_at: datetime

    @property
    def spec(self) -> dict[str, Any]:
        return {"name": self.algo, "params": dict(self.params)}


def settings_recorded(state: SqliteState) -> bool:
    """Whether the state DB has the settings table (migration 054)."""
    return bool(
        state.sql(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'execution_algo_settings'"
        )
    )


def _setting(row: Any) -> AlgoSetting:
    return AlgoSetting(
        portfolio_id=row["portfolio_id"],
        strategy_id=row["strategy_id"] or None,
        algo=row["algo"],
        params=json.loads(row["params_json"] or "{}"),
        updated_by=row["updated_by"],
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def list_settings(state: SqliteState, portfolio_id: str) -> list[AlgoSetting]:
    """The portfolio's own setting first, then each strategy's."""
    if not settings_recorded(state):
        return []
    rows = state.sql(
        "SELECT * FROM execution_algo_settings WHERE portfolio_id = ? ORDER BY strategy_id",
        [portfolio_id],
    )
    return [_setting(r) for r in rows]


def set_setting(
    state: SqliteState,
    portfolio_id: str,
    algo: str,
    params: Mapping[str, Any] | None = None,
    *,
    strategy_id: str | None = None,
    actor: str | None,
    now: datetime,
) -> AlgoSetting:
    """Store the algo for the portfolio (or one strategy in it). The
    parameters are checked (``AlgoParamsError``) and stored with their
    defaults filled in."""
    spec = algo_spec(algo, params)
    state.execute(
        "INSERT INTO execution_algo_settings"
        " (portfolio_id, strategy_id, algo, params_json, updated_by, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)"
        " ON CONFLICT (portfolio_id, strategy_id) DO UPDATE SET algo = excluded.algo,"
        " params_json = excluded.params_json, updated_by = excluded.updated_by,"
        " updated_at = excluded.updated_at",
        [
            portfolio_id,
            strategy_id or PORTFOLIO_ROW,
            spec["name"],
            json.dumps(spec["params"], sort_keys=True),
            actor,
            now.isoformat(timespec="seconds"),
        ],
    )
    rows = state.sql(
        "SELECT * FROM execution_algo_settings WHERE portfolio_id = ? AND strategy_id = ?",
        [portfolio_id, strategy_id or PORTFOLIO_ROW],
    )
    return _setting(rows[0])


def clear_setting(state: SqliteState, portfolio_id: str, strategy_id: str | None = None) -> bool:
    """Back to plain orders (or to the portfolio's setting, for a
    strategy). Returns whether a row was removed."""
    cur = state.execute(
        "DELETE FROM execution_algo_settings WHERE portfolio_id = ? AND strategy_id = ?",
        [portfolio_id, strategy_id or PORTFOLIO_ROW],
    )
    return cur.rowcount > 0


def resolve_algo(
    state: SqliteState, portfolio_id: str, strategy_id: str | None
) -> dict[str, Any] | None:
    """The algo for one strategy's orders in a portfolio: the strategy's
    row, else the portfolio's, else ``None`` (plain)."""
    return _Resolver(state, portfolio_id).get(strategy_id)


def works_with_algo(order: Order) -> bool:
    """Orders an algo may work: not a stop, not an option, not in a
    one-cancels-other group."""
    return (
        order.order_type in ("market", "limit")
        and not is_option_id(order.ticker)
        and order.oca_group is None
    )


def attach_algos(state: SqliteState, orders: Sequence[Order], portfolio_id: str) -> list[Order]:
    """``orders`` with the resolved algo on each one that has none and
    that an algo may work (:func:`works_with_algo`)."""
    resolver = _Resolver(state, portfolio_id)
    out: list[Order] = []
    for order in orders:
        if order.algo is None and works_with_algo(order):
            spec = resolver.get(order.strategy_id)
            if spec is not None:
                order = replace(order, algo=spec)
        out.append(order)
    return out


class _Resolver:
    """The portfolio's settings, read once."""

    def __init__(self, state: SqliteState, portfolio_id: str) -> None:
        rows = list_settings(state, portfolio_id)
        self._own = next((r for r in rows if r.strategy_id is None), None)
        self._by_strategy = {r.strategy_id: r for r in rows if r.strategy_id is not None}

    def get(self, strategy_id: str | None) -> dict[str, Any] | None:
        row = self._by_strategy.get(strategy_id) if strategy_id else None
        row = row or self._own
        return row.spec if row is not None else None
