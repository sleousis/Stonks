"""StatementImportService: CSV statements for brokers without an API
(roadmap 23.17).

You map the columns of your broker's CSV export onto trades, dividends and
cash flows (:mod:`stonks.connections.statement_csv`). The rows land in the
same tables a broker connection fills: ``broker_activities`` for the
activities, and a ``sync`` snapshot with ``broker_positions`` for the
holdings the trades add up to. Insights, cash flows and tax reports read
them like any synced account.

- **Preview first.** ``preview`` writes nothing. It shows each row as new,
  duplicate or skipped (with the reason), and the mapping it used (a guess
  from the headers when you gave none).
- **Duplicate protection.** Each row has a stable id. A row already
  imported into your CSV connection is a duplicate and is not written
  again, so importing the same file twice changes nothing.
- **Undo.** Every commit is one ``statement_imports`` row, and the rows it
  added carry its id. Undo deletes exactly those and rebuilds the holdings.
- The imports live on one connection per person (provider ``csv``) that
  never syncs. The target is a broker portfolio: an existing one linked to
  that connection, or a new one made on the first import.

Reading your imports needs ``data.read``. Preview, import and undo need
``portfolio.manage``.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from stonks.accounts import NotFound, PortfolioRepository
from stonks.accounts.audit import AuditLog
from stonks.accounts.scope import Scope, owned_portfolio
from stonks.app.context import AppContext
from stonks.app.errors import ConflictError, NotFoundError, ValidationError
from stonks.auth.policy import Permission, require
from stonks.auth.principal import Principal
from stonks.connections.base import Activity
from stonks.connections.statement_csv import (
    ColumnMapping,
    ParsedRow,
    StatementError,
    guess_mapping,
    parse_statement,
    read_headers,
)
from stonks.logging import get_logger
from stonks.store.state import SqliteState

_log = get_logger("stonks.app.statement_imports")

#: The provider name of the per-person connection that holds CSV imports.
CSV_PROVIDER = "csv"
#: Largest CSV accepted, in characters.
MAX_CONTENT = 5_000_000
#: Rows a preview shows (the counts cover the whole file).
PREVIEW_ROWS = 200
#: A CSV connection never syncs.
_NEVER = "9999-12-31T00:00:00+00:00"

RowStatus = Literal["new", "duplicate", "skipped"]


class StatementImportRequest(BaseModel):
    """A CSV to preview or import, into ``portfolio_id`` (a broker portfolio
    of yours) or into a new portfolio named ``new_portfolio``."""

    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=MAX_CONTENT, description="The CSV text.")
    filename: str | None = Field(default=None, max_length=200)
    mapping: ColumnMapping | None = Field(
        default=None, description="Column mapping; blank guesses it from the headers."
    )
    portfolio_id: str | None = Field(default=None, max_length=64)
    new_portfolio: str | None = Field(default=None, min_length=1, max_length=80)
    #: The currency of a new portfolio.
    currency: str = Field(default="USD", min_length=3, max_length=3)

    @model_validator(mode="after")
    def _target(self) -> Self:
        if (self.portfolio_id is None) == (self.new_portfolio is None):
            raise ValueError("give a portfolio_id or a new_portfolio name, not both")
        return self


class StatementRowView(BaseModel):
    line: int
    status: RowStatus
    reason: str | None = None
    kind: str | None = None
    day: date | None = None
    symbol: str | None = None
    ticker: str | None = None
    quantity: float | None = None
    price: float | None = None
    amount: float | None = None
    fee: float | None = None
    currency: str | None = None


class StatementPreview(BaseModel):
    headers: list[str]
    mapping: ColumnMapping
    #: True when the mapping was guessed from the headers.
    guessed: bool
    rows: list[StatementRowView]
    total: int
    new: int
    duplicate: int
    skipped: int
    first_date: date | None
    last_date: date | None
    #: Symbols no ticker maps to ("not covered"); they are kept by symbol.
    unmapped: list[str]


class StatementImportView(BaseModel):
    id: str
    portfolio_id: str
    portfolio_name: str | None
    filename: str | None
    rows_total: int
    rows_added: int
    rows_duplicate: int
    rows_skipped: int
    first_date: date | None
    last_date: date | None
    created_at: datetime
    undone_at: datetime | None


def _import_view(row: sqlite3.Row) -> StatementImportView:
    r = dict(row)
    return StatementImportView(
        id=r["id"],
        portfolio_id=r["portfolio_id"],
        portfolio_name=r.get("portfolio_name"),
        filename=r["filename"],
        rows_total=r["rows_total"],
        rows_added=r["rows_added"],
        rows_duplicate=r["rows_duplicate"],
        rows_skipped=r["rows_skipped"],
        first_date=date.fromisoformat(r["first_date"]) if r["first_date"] else None,
        last_date=date.fromisoformat(r["last_date"]) if r["last_date"] else None,
        created_at=datetime.fromisoformat(r["created_at"]),
        undone_at=datetime.fromisoformat(r["undone_at"]) if r["undone_at"] else None,
    )


class StatementImportService:
    def __init__(self, context: AppContext, *, clock: Callable[[], datetime] | None = None) -> None:
        self._ctx = context
        self._clock = clock or (lambda: datetime.now(UTC))

    # ---- reads -------------------------------------------------------------------------

    def list(self, principal: Principal) -> list[StatementImportView]:
        """Your imports, newest first."""
        require(principal, Permission.READ)
        with self._ctx.state() as state:
            rows = state.sql(
                "SELECT i.*, p.name AS portfolio_name FROM statement_imports i"
                " LEFT JOIN portfolios p ON p.id = i.portfolio_id"
                " WHERE i.user_id = ? ORDER BY i.created_at DESC, i.id DESC",
                [principal.user_id],
            )
        return [_import_view(r) for r in rows]

    # ---- preview and import ------------------------------------------------------------

    def preview(self, principal: Principal, body: StatementImportRequest) -> StatementPreview:
        """What an import would do. Writes nothing."""
        require(principal, Permission.PORTFOLIO_MANAGE)
        scope = _person(principal)
        mapping, guessed, parsed = self._parse(body)
        with self._ctx.state() as state:
            if body.portfolio_id is not None:
                self._target(state, scope, body.portfolio_id)
            known = self._known(state, scope.user_id)
        return _preview(read_headers(body.content), mapping, guessed, parsed, known)

    def commit(self, principal: Principal, body: StatementImportRequest) -> StatementImportView:
        """Import the new rows. Duplicates and skipped rows are counted,
        never written."""
        require(principal, Permission.PORTFOLIO_MANAGE)
        scope = _person(principal)
        mapping, _, parsed = self._parse(body)
        now = self._clock()
        with self._ctx.state() as state, state.transaction():
            connection_id = self._connection(state, scope, now)
            if body.portfolio_id is not None:
                portfolio_id, account_id = self._target(state, scope, body.portfolio_id)
            else:
                portfolio_id, account_id = self._new_portfolio(
                    state, scope, connection_id, body.new_portfolio or "", body.currency, now
                )
            known = self._known(state, scope.user_id)
            new = [r.activity for r in parsed if r.activity is not None]
            fresh: list[Activity] = []
            for a in new:
                if a.provider_activity_id not in known:
                    known.add(a.provider_activity_id)
                    fresh.append(a)
            if not fresh:
                raise ConflictError("nothing new to import: every row is a duplicate or skipped")
            import_id = f"imp_{secrets.token_hex(6)}"
            days = sorted(a.trade_date for a in fresh if a.trade_date)
            state.execute(
                "INSERT INTO statement_imports (id, user_id, portfolio_id, connection_id,"
                " filename, mapping_json, rows_total, rows_added, rows_duplicate, rows_skipped,"
                " first_date, last_date, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    import_id,
                    scope.user_id,
                    portfolio_id,
                    connection_id,
                    body.filename,
                    mapping.model_dump_json(),
                    len(parsed),
                    len(fresh),
                    len(new) - len(fresh),
                    sum(1 for r in parsed if r.activity is None),
                    days[0].isoformat() if days else None,
                    days[-1].isoformat() if days else None,
                    _iso(now),
                ],
            )
            for a in fresh:
                state.execute(
                    "INSERT INTO broker_activities (connection_id, portfolio_id,"
                    " external_account_id, provider_activity_id, kind, raw_symbol, ticker,"
                    " quantity, price, amount, fee, currency, trade_date, description,"
                    " synced_at, import_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        connection_id,
                        portfolio_id,
                        account_id,
                        a.provider_activity_id,
                        a.kind,
                        a.raw_symbol,
                        a.ticker,
                        a.quantity,
                        a.price,
                        a.amount,
                        a.fee,
                        a.currency,
                        a.trade_date.isoformat() if a.trade_date else None,
                        a.description,
                        _iso(now),
                        import_id,
                    ],
                )
            _rebuild_holdings(state, portfolio_id, now)
            AuditLog(state).record(
                scope.actor,
                "statement.import",
                "statement_import",
                import_id,
                portfolio_id=portfolio_id,
                details={"rows_added": len(fresh), "filename": body.filename},
            )
            _log.info("statement_imports.committed", import_id=import_id, rows=len(fresh))
            return self._get(state, scope.user_id, import_id)

    def undo(self, principal: Principal, import_id: str) -> StatementImportView:
        """Remove exactly the rows one import added, and rebuild the holdings."""
        require(principal, Permission.PORTFOLIO_MANAGE)
        scope = _person(principal)
        now = self._clock()
        with self._ctx.state() as state, state.transaction():
            view = self._get(state, scope.user_id, import_id)
            if view.undone_at is not None:
                raise ConflictError("that import is already undone")
            state.execute("DELETE FROM broker_activities WHERE import_id = ?", [import_id])
            state.execute(
                "UPDATE statement_imports SET undone_at = ? WHERE id = ?", [_iso(now), import_id]
            )
            _rebuild_holdings(state, view.portfolio_id, now)
            AuditLog(state).record(
                scope.actor,
                "statement.undo",
                "statement_import",
                import_id,
                portfolio_id=view.portfolio_id,
                details={"rows_removed": view.rows_added},
            )
            return self._get(state, scope.user_id, import_id)

    # ---- helpers -----------------------------------------------------------------------

    @staticmethod
    def _parse(body: StatementImportRequest) -> tuple[ColumnMapping, bool, list[ParsedRow]]:
        guessed = body.mapping is None
        try:
            mapping = body.mapping or guess_mapping(read_headers(body.content))
            return mapping, guessed, parse_statement(body.content, mapping)
        except StatementError as exc:
            raise ValidationError(str(exc)) from None
        except ValueError as exc:  # a guessed mapping that does not validate
            raise ValidationError(f"could not read the CSV: {exc}") from None

    @staticmethod
    def _known(state: SqliteState, user_id: str) -> set[str]:
        return {
            r[0]
            for r in state.sql(
                "SELECT a.provider_activity_id FROM broker_activities a"
                " JOIN broker_connections c ON c.id = a.connection_id"
                " WHERE c.user_id = ? AND c.provider = ?",
                [user_id, CSV_PROVIDER],
            )
        }

    @staticmethod
    def _connection(state: SqliteState, scope: Scope, now: datetime) -> str:
        rows = state.sql(
            "SELECT id FROM broker_connections WHERE user_id = ? AND provider = ?",
            [scope.user_id, CSV_PROVIDER],
        )
        if rows:
            return str(rows[0]["id"])
        connection_id = f"con_{secrets.token_hex(8)}"
        state.execute(
            "INSERT INTO broker_connections (id, user_id, provider, label, status,"
            " next_sync_at, created_at, updated_at) VALUES (?, ?, ?, ?, 'active', ?, ?, ?)",
            [
                connection_id,
                scope.user_id,
                CSV_PROVIDER,
                "CSV statements",
                _NEVER,
                _iso(now),
                _iso(now),
            ],
        )
        return connection_id

    @staticmethod
    def _target(state: SqliteState, scope: Scope, portfolio_id: str) -> tuple[str, str]:
        try:
            portfolio = owned_portfolio(state, scope, portfolio_id)
        except NotFound:
            raise NotFoundError(f"portfolio {portfolio_id!r} not found") from None
        rows = state.sql(
            "SELECT p.external_account_id, c.provider FROM portfolios p"
            " LEFT JOIN broker_connections c ON c.id = p.broker_connection_id WHERE p.id = ?",
            [portfolio.id],
        )
        if not rows or rows[0]["provider"] != CSV_PROVIDER:
            raise ValidationError(
                "import into a portfolio made by an earlier CSV import, or name a new one"
            )
        return portfolio.id, str(rows[0]["external_account_id"])

    @staticmethod
    def _new_portfolio(
        state: SqliteState,
        scope: Scope,
        connection_id: str,
        name: str,
        currency: str,
        now: datetime,
    ) -> tuple[str, str]:
        name = name.strip()
        if state.sql(
            "SELECT 1 FROM portfolios WHERE owner_id = ? AND name = ?", [scope.user_id, name]
        ):
            raise ConflictError(f"you already have a portfolio named {name!r}")
        account_id = f"csv-{secrets.token_hex(4)}"
        state.execute(
            "INSERT INTO broker_accounts (connection_id, external_account_id, name,"
            " institution, currency, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [connection_id, account_id, name, "CSV import", currency.upper(), _iso(now), _iso(now)],
        )
        portfolio = PortfolioRepository(state).create(
            scope, name=name, kind="broker", base_currency=currency.upper()
        )
        state.execute(
            "UPDATE portfolios SET broker_connection_id = ?, external_account_id = ? WHERE id = ?",
            [connection_id, account_id, portfolio.id],
        )
        return portfolio.id, account_id

    @staticmethod
    def _get(state: SqliteState, user_id: str, import_id: str) -> StatementImportView:
        rows = state.sql(
            "SELECT i.*, p.name AS portfolio_name FROM statement_imports i"
            " LEFT JOIN portfolios p ON p.id = i.portfolio_id WHERE i.id = ? AND i.user_id = ?",
            [import_id, user_id],
        )
        if not rows:
            raise NotFoundError(f"no import with id {import_id!r}")
        return _import_view(rows[0])


def _person(principal: Principal) -> Scope:
    if principal.scope.is_service:
        raise ValidationError("statements are imported by people, not services")
    return principal.scope


def _preview(
    headers: list[str],
    mapping: ColumnMapping,
    guessed: bool,
    parsed: list[ParsedRow],
    known: set[str],
) -> StatementPreview:
    rows: list[StatementRowView] = []
    counts = {"new": 0, "duplicate": 0, "skipped": 0}
    days: list[date] = []
    unmapped: set[str] = set()
    for r in parsed:
        a = r.activity
        if a is None:
            status: RowStatus = "skipped"
        elif a.provider_activity_id in known:
            status = "duplicate"
        else:
            status = "new"
            known = known | {a.provider_activity_id}
        counts[status] += 1
        if a is not None and a.trade_date:
            days.append(a.trade_date)
        if a is not None and a.raw_symbol and a.ticker is None:
            unmapped.add(a.raw_symbol)
        if len(rows) < PREVIEW_ROWS:
            rows.append(
                StatementRowView(
                    line=r.line,
                    status=status,
                    reason=r.skipped,
                    kind=a.kind if a else None,
                    day=a.trade_date if a else None,
                    symbol=a.raw_symbol if a else None,
                    ticker=a.ticker if a else None,
                    quantity=a.quantity if a else None,
                    price=a.price if a else None,
                    amount=a.amount if a else None,
                    fee=a.fee if a else None,
                    currency=a.currency if a else None,
                )
            )
    return StatementPreview(
        headers=headers,
        mapping=mapping,
        guessed=guessed,
        rows=rows,
        total=len(parsed),
        new=counts["new"],
        duplicate=counts["duplicate"],
        skipped=counts["skipped"],
        first_date=min(days) if days else None,
        last_date=max(days) if days else None,
        unmapped=sorted(unmapped),
    )


def _rebuild_holdings(state: SqliteState, portfolio_id: str, now: datetime) -> None:
    """One ``sync`` snapshot of what the imported activities add up to:
    net quantity per symbol at its last trade price, and the cash their
    amounts leave. A CSV portfolio has no other snapshots."""
    rows = state.sql(
        "SELECT kind, raw_symbol, ticker, quantity, price, amount, currency FROM broker_activities"
        " WHERE portfolio_id = ? ORDER BY trade_date, id",
        [portfolio_id],
    )
    state.execute(
        "DELETE FROM portfolio_snapshots WHERE portfolio_id = ? AND source = 'sync'",
        [portfolio_id],
    )
    if not rows:
        return
    cash = 0.0
    held: dict[str, dict[str, object]] = {}
    for r in rows:
        cash += float(r["amount"] or 0.0)
        if r["kind"] == "trade" and r["raw_symbol"]:
            pos = held.setdefault(
                r["raw_symbol"], {"ticker": r["ticker"], "qty": 0.0, "price": None, "ccy": None}
            )
            pos["qty"] = float(pos["qty"]) + float(r["quantity"] or 0.0)  # type: ignore[arg-type]
            if r["price"] is not None:
                pos["price"] = float(r["price"])
            pos["ccy"] = r["currency"]
    open_positions = {s: p for s, p in held.items() if abs(float(p["qty"])) > 1e-9}  # type: ignore[arg-type]
    value = sum(
        float(p["qty"]) * float(p["price"] or 0.0)  # type: ignore[arg-type]
        for p in open_positions.values()
    )
    mapped = {
        str(p["ticker"]): float(p["qty"])  # type: ignore[arg-type]
        for p in open_positions.values()
        if p["ticker"]
    }
    state.execute(
        "INSERT INTO portfolio_snapshots (tick_id, as_of, taken_at, cash, positions_json,"
        " total_value, portfolio_id, source) VALUES (NULL, ?, ?, ?, ?, ?, ?, 'sync')",
        [
            now.date().isoformat(),
            _iso(now),
            cash,
            json.dumps(mapped, sort_keys=True),
            cash + value,
            portfolio_id,
        ],
    )
    snapshot_id = int(
        state.sql(
            "SELECT id FROM portfolio_snapshots WHERE portfolio_id = ? AND source = 'sync'",
            [portfolio_id],
        )[0][0]
    )
    for symbol, p in open_positions.items():
        qty = float(p["qty"])  # type: ignore[arg-type]
        price = float(p["price"]) if p["price"] is not None else None  # type: ignore[arg-type]
        state.execute(
            "INSERT INTO broker_positions (snapshot_id, portfolio_id, raw_symbol, ticker,"
            " quantity, price, market_value, currency, description)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'from CSV statements')",
            [
                snapshot_id,
                portfolio_id,
                symbol,
                p["ticker"],
                qty,
                price,
                qty * price if price is not None else None,
                p["ccy"],
            ],
        )


def _iso(ts: datetime) -> str:
    return ts.isoformat(timespec="seconds")
