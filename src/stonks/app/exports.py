"""ExportService: CSV downloads (roadmap 13.12).

Orders, fills, the trade journal, portfolio snapshots and daily P&L of one
of the caller's portfolios, and the lab's trial results. The portfolio is
resolved against the caller by the route (``PortfolioService.resolve``), so
another person's book is a 404. Trial results need ``lab.run``, like the
trial ledger.

Every file is plain RFC 4180 CSV with a header row, UTF-8, newest rows first
where the screen shows them that way. Text that a spreadsheet would read as
a formula (``=``, ``+``, ``-``, ``@`` first) is prefixed with ``'``.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, date, datetime
from typing import Any, Literal

from stonks.app.context import AppContext
from stonks.app.orders import OrdersService
from stonks.app.portfolio import PortfolioService
from stonks.app.tca import TcaService
from stonks.auth.policy import Permission, require
from stonks.auth.principal import Principal
from stonks.production.pnl import load_pnl

ExportKind = Literal["orders", "fills", "journal", "snapshots", "pnl", "lab-trials"]

#: Rows in one file at most; a longer history is cut to the newest rows.
MAX_EXPORT_ROWS = 100_000
_PAGE = 5_000
_FORMULA = ("=", "+", "-", "@", "\t", "\r")


def _cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        value = json.dumps(value, sort_keys=True, default=str)
    if isinstance(value, str) and value.startswith(_FORMULA):
        return "'" + value
    return value


def to_csv(header: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow([_cell(v) for v in row])
    return buf.getvalue()


def filename(kind: ExportKind, scope: str, today: date | None = None) -> str:
    """``stonks-<kind>-<portfolio or all>-<YYYY-MM-DD>.csv``, safe for a
    Content-Disposition header."""
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in scope)[:64] or "all"
    return f"stonks-{kind}-{safe}-{(today or datetime.now(UTC).date()).isoformat()}.csv"


class ExportService:
    def __init__(self, context: AppContext, portfolio: PortfolioService) -> None:
        self._ctx = context
        self._portfolio = portfolio
        self._orders = OrdersService(context)
        self._tca = TcaService(context)

    def orders(self, portfolio_id: str) -> str:
        header = [
            "client_id",
            "created_at",
            "updated_at",
            "strategy_id",
            "ticker",
            "side",
            "quantity",
            "order_type",
            "limit_price",
            "status",
            "status_reason",
            "tick_id",
            "broker_order_id",
        ]
        rows = self._paged(
            lambda limit, offset: (
                self._orders.orders(limit=limit, offset=offset, portfolio_id=portfolio_id).items
            )
        )
        return to_csv(header, ([getattr(o, h) for h in header] for o in rows))

    def fills(self, portfolio_id: str) -> str:
        header = [
            "id",
            "filled_at",
            "order_client_id",
            "ticker",
            "side",
            "quantity",
            "price",
            "fee",
            "tick_id",
        ]
        rows = self._paged(
            lambda limit, offset: (
                self._orders.fills(limit=limit, offset=offset, portfolio_id=portfolio_id).items
            )
        )
        return to_csv(header, ([getattr(f, h) for h in header] for f in rows))

    def journal(self, portfolio_id: str) -> str:
        header = [
            "client_id",
            "created_at",
            "decided_at",
            "strategy_id",
            "ticker",
            "side",
            "quantity",
            "status",
            "status_reason",
            "trigger",
            "decision_price",
            "shortfall_bps",
            "total_cost_bps",
            "next_session_move_bps",
            "notes",
        ]
        entries = self._paged(
            lambda limit, offset: self._tca.journal(portfolio_id, limit=limit, offset=offset).items
        )
        return to_csv(
            header,
            (
                [
                    e.client_id,
                    e.created_at,
                    e.decided_at,
                    e.strategy_id,
                    e.ticker,
                    e.side,
                    e.quantity,
                    e.status,
                    e.status_reason,
                    e.trigger,
                    e.decision_price,
                    e.shortfall.is_bps if e.shortfall else None,
                    e.shortfall.total_bps if e.shortfall else None,
                    e.next_session_move_bps,
                    " | ".join(n.note for n in e.notes),
                ]
                for e in entries
            ),
        )

    def snapshots(self, portfolio_id: str) -> str:
        header = ["id", "taken_at", "tick_id", "cash", "total_value", "positions"]
        rows = self._paged(
            lambda limit, offset: (
                self._portfolio.snapshots(
                    limit=limit, offset=offset, portfolio_id=portfolio_id
                ).items
            )
        )
        return to_csv(
            header,
            (
                [s.id, s.taken_at.isoformat(), s.tick_id, s.cash, s.total_value, s.positions]
                for s in rows
            ),
        )

    def pnl(self, portfolio_id: str, since: date | None = None) -> str:
        header = [
            "day",
            "total_value",
            "daily_change",
            "daily_return",
            "cumulative_return",
            "drawdown",
            "days_elapsed",
        ]
        with self._ctx.state() as state:
            rows = load_pnl(state, since=since, portfolio_id=portfolio_id)
        return to_csv(
            header,
            (
                [
                    r.day.isoformat(),
                    r.total_value,
                    r.daily_change,
                    r.daily_return,
                    r.cumulative_return,
                    r.drawdown,
                    r.days_elapsed,
                ]
                for r in rows[-MAX_EXPORT_ROWS:]
            ),
        )

    def lab_trials(self, principal: Principal, run_id: str | None = None) -> str:
        require(principal, Permission.LAB_RUN)
        header = [
            "run_id",
            "strategy_class",
            "started_at",
            "verdict",
            "trial_index",
            "status",
            "score",
            "n_bars",
            "params",
        ]
        where, params = ("WHERE r.id = ?", [run_id]) if run_id else ("", [])
        with self._ctx.state() as state:
            rows = state.sql(
                "SELECT r.id AS run_id, r.strategy_class, r.started_at, r.verdict,"
                " t.trial_index, t.status, t.score, t.n_bars, t.params_json"
                f" FROM lab_trials t JOIN lab_runs r ON r.id = t.run_id {where}"
                " ORDER BY r.started_at DESC, r.id DESC, t.trial_index LIMIT ?",
                [*params, MAX_EXPORT_ROWS],
            )
        return to_csv(
            header,
            (
                [
                    r["run_id"],
                    r["strategy_class"],
                    r["started_at"],
                    r["verdict"],
                    r["trial_index"],
                    r["status"],
                    r["score"],
                    r["n_bars"],
                    json.loads(r["params_json"] or "{}"),
                ]
                for r in rows
            ),
        )

    @staticmethod
    def _paged[T](fetch: Callable[[int, int], Sequence[T]]) -> list[T]:
        out: list[T] = []
        while len(out) < MAX_EXPORT_ROWS:
            page = fetch(_PAGE, len(out))
            out.extend(page)
            if len(page) < _PAGE:
                break
        return out[:MAX_EXPORT_ROWS]
