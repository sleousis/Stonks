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
- **Broker presets.** A known broker export (DEGIRO Transactions, Account
  statement and Portfolio) needs no mapping: its headers are recognised in
  every language the broker writes, and instruments map to tickers by ISIN
  (:mod:`stonks.connections.statement_presets`). A holdings export sets the
  holdings and cash on its day instead of adding activities.
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
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Literal, Self

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
from stonks.connections.statement_presets import registry as preset_registry
from stonks.connections.statement_presets.base import (
    ParsedHolding,
    PresetKind,
    PresetParse,
    StatementPreset,
)
from stonks.connections.statement_presets.lake_resolver import LakeInstrumentResolver
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
#: ``preset`` value that asks for detection from the headers.
AUTO = "auto"
#: ``preset`` value that skips the presets: map the columns (or guess them).
NO_PRESET = "none"


class StatementImportRequest(BaseModel):
    """A CSV to preview or import, into ``portfolio_id`` (a broker portfolio
    of yours) or into a new portfolio named ``new_portfolio``."""

    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=MAX_CONTENT, description="The CSV text.")
    filename: str | None = Field(default=None, max_length=200)
    mapping: ColumnMapping | None = Field(
        default=None, description="Column mapping; blank guesses it from the headers."
    )
    preset: str | None = Field(
        default=None,
        max_length=64,
        description="A broker export preset id (GET /api/statement-imports/presets), 'auto'"
        " to find one from the headers, or 'none' to map the columns. Blank with no mapping"
        " also finds a preset first.",
    )
    as_of: date | None = Field(
        default=None, description="The day a holdings export describes; blank is today."
    )
    portfolio_id: str | None = Field(default=None, max_length=64)
    new_portfolio: str | None = Field(default=None, min_length=1, max_length=80)
    #: The currency of a new portfolio.
    currency: str = Field(default="USD", min_length=3, max_length=3)

    @model_validator(mode="after")
    def _target(self) -> Self:
        if (self.portfolio_id is None) == (self.new_portfolio is None):
            raise ValueError("give a portfolio_id or a new_portfolio name, not both")
        if self.mapping is not None and self.preset not in (None, AUTO, NO_PRESET):
            raise ValueError("give a column mapping or a preset, not both")
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


class StatementPresetView(BaseModel):
    """A broker export Stonks reads without a mapping."""

    id: str
    broker: str
    label: str
    kind: PresetKind
    how_to_export: str


class StatementPreview(BaseModel):
    headers: list[str]
    #: The column mapping used; ``None`` when a preset read the file.
    mapping: ColumnMapping | None
    #: True when the mapping was guessed from the headers.
    guessed: bool
    #: The broker export preset that read the file, if any.
    preset: str | None = None
    preset_label: str | None = None
    #: The file's language when a preset read it (``en``, ``nl``, ``de``, ...).
    locale: str | None = None
    #: ``holdings`` for an export of positions on one day.
    kind: PresetKind = "activities"
    #: The day a holdings export describes.
    as_of: date | None = None
    #: What the preset left out on purpose, and why.
    notes: list[str] = Field(default_factory=list)
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
    preset: str | None = None
    kind: PresetKind = "activities"
    as_of: date | None = None
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
        preset=r.get("preset"),
        kind=r.get("kind") or "activities",
        as_of=date.fromisoformat(r["as_of"]) if r.get("as_of") else None,
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

    def presets(self, principal: Principal) -> list[StatementPresetView]:
        """The broker exports Stonks reads without a column mapping."""
        require(principal, Permission.READ)
        return [
            StatementPresetView(
                id=p.id, broker=p.broker, label=p.label, kind=p.kind, how_to_export=p.how_to_export
            )
            for p in preset_registry.presets()
        ]

    # ---- preview and import ------------------------------------------------------------

    def preview(self, principal: Principal, body: StatementImportRequest) -> StatementPreview:
        """What an import would do. Writes nothing."""
        require(principal, Permission.PORTFOLIO_MANAGE)
        scope = _person(principal)
        reading = self._read(body)
        with self._ctx.state() as state:
            if body.portfolio_id is not None:
                self._target(state, scope, body.portfolio_id)
            if reading.kind == "holdings":
                known = self._known_holdings(state, scope.user_id)
            else:
                known = self._known(state, scope.user_id)
        return _preview(read_headers(body.content), reading, known, self._clock().date())

    def commit(self, principal: Principal, body: StatementImportRequest) -> StatementImportView:
        """Import the new rows. Duplicates and skipped rows are counted,
        never written."""
        require(principal, Permission.PORTFOLIO_MANAGE)
        scope = _person(principal)
        reading = self._read(body)
        now = self._clock()
        with self._ctx.state() as state, state.transaction():
            connection_id = self._connection(state, scope, now)
            if body.portfolio_id is not None:
                portfolio_id, account_id = self._target(state, scope, body.portfolio_id)
            else:
                portfolio_id, account_id = self._new_portfolio(
                    state, scope, connection_id, body.new_portfolio or "", body.currency, now
                )
            target = _Target(scope.user_id, portfolio_id, connection_id, account_id)
            import_id = f"imp_{secrets.token_hex(6)}"
            if reading.kind == "holdings":
                added = self._commit_holdings(state, target, reading, import_id, body, now)
            else:
                added = self._commit_activities(state, target, reading, import_id, body, now)
            _rebuild_holdings(state, portfolio_id, now)
            preset_id = reading.preset.id if reading.preset else None
            AuditLog(state).record(
                scope.actor,
                "statement.import",
                "statement_import",
                import_id,
                portfolio_id=portfolio_id,
                details={"rows_added": added, "filename": body.filename, "preset": preset_id},
            )
            _log.info(
                "statement_imports.committed", import_id=import_id, rows=added, preset=preset_id
            )
            return self._get(state, scope.user_id, import_id)

    def _commit_activities(
        self,
        state: SqliteState,
        target: _Target,
        reading: _Reading,
        import_id: str,
        body: StatementImportRequest,
        now: datetime,
    ) -> int:
        known = self._known(state, target.user_id)
        new = [r.activity for r in reading.rows if r.activity is not None]
        fresh: list[Activity] = []
        for a in new:
            if a.provider_activity_id not in known:
                known.add(a.provider_activity_id)
                fresh.append(a)
        if not fresh:
            raise ConflictError("nothing new to import: every row is a duplicate or skipped")
        days = sorted(a.trade_date for a in fresh if a.trade_date)
        skipped = sum(1 for r in reading.rows if r.activity is None)
        _insert_import(
            state,
            import_id,
            target,
            body,
            reading,
            counts=(len(reading.rows), len(fresh), len(new) - len(fresh), skipped),
            days=(days[0] if days else None, days[-1] if days else None),
            as_of=None,
            now=now,
        )
        for a in fresh:
            state.execute(
                "INSERT INTO broker_activities (connection_id, portfolio_id,"
                " external_account_id, provider_activity_id, kind, raw_symbol, ticker,"
                " quantity, price, amount, fee, currency, trade_date, description,"
                " synced_at, import_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    target.connection_id,
                    target.portfolio_id,
                    target.account_id,
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
        return len(fresh)

    def _commit_holdings(
        self,
        state: SqliteState,
        target: _Target,
        reading: _Reading,
        import_id: str,
        body: StatementImportRequest,
        now: datetime,
    ) -> int:
        parsed = reading.parsed
        assert parsed is not None
        as_of = reading.as_of or now.date()
        known = self._known_holdings(state, target.user_id)
        fresh = [h for h in parsed.holdings if _holding_id(as_of, h) not in known]
        if not fresh:
            raise ConflictError("nothing new to import: these holdings are already imported")
        _insert_import(
            state,
            import_id,
            target,
            body,
            reading,
            counts=(
                parsed.total,
                len(fresh),
                len(parsed.holdings) - len(fresh),
                len(parsed.skipped),
            ),
            days=(as_of, as_of),
            as_of=as_of,
            now=now,
        )
        for h in fresh:
            state.execute(
                "INSERT INTO statement_import_holdings (import_id, row_id, raw_symbol, ticker,"
                " quantity, price, market_value, currency, description, is_cash)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    import_id,
                    _holding_id(as_of, h),
                    h.raw_symbol,
                    h.ticker,
                    h.quantity,
                    h.price,
                    h.market_value,
                    h.currency,
                    h.description,
                    1 if h.is_cash else 0,
                ],
            )
        return len(fresh)

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
            state.execute("DELETE FROM statement_import_holdings WHERE import_id = ?", [import_id])
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

    def _read(self, body: StatementImportRequest) -> _Reading:
        """Read the file with the mapping, else the named preset, else a
        preset known from the headers, else a mapping guessed from them."""
        try:
            if body.mapping is not None:
                return _Reading(body.mapping, False, parse_statement(body.content, body.mapping))
            chosen: StatementPreset | None = None
            if body.preset == NO_PRESET:
                chosen = None
            elif body.preset not in (None, AUTO):
                chosen = preset_registry.preset(body.preset or "")
            else:
                found = preset_registry.detect(read_headers(body.content, sniff=True))
                chosen = found[0] if found else None
            if chosen is not None:
                parsed = chosen.parse(body.content, LakeInstrumentResolver(self._ctx.lake))
                return _Reading(None, False, parsed.rows, chosen, parsed, body.as_of)
            if body.preset == AUTO:
                raise StatementError("no broker preset knows these headers; map the columns")
            mapping = guess_mapping(read_headers(body.content))
            return _Reading(mapping, True, parse_statement(body.content, mapping))
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
    def _known_holdings(state: SqliteState, user_id: str) -> set[str]:
        return {
            r[0]
            for r in state.sql(
                "SELECT h.row_id FROM statement_import_holdings h"
                " JOIN statement_imports i ON i.id = h.import_id WHERE i.user_id = ?",
                [user_id],
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


@dataclass(frozen=True)
class _Target:
    user_id: str
    portfolio_id: str
    connection_id: str
    account_id: str


@dataclass
class _Reading:
    """A file read by a column mapping, or by a broker preset."""

    mapping: ColumnMapping | None
    guessed: bool
    rows: list[ParsedRow]
    preset: StatementPreset | None = None
    parsed: PresetParse | None = None
    as_of: date | None = None

    @property
    def kind(self) -> PresetKind:
        return self.parsed.kind if self.parsed is not None else "activities"


def _holding_id(as_of: date, holding: ParsedHolding) -> str:
    """A holdings line is new per export day: the same file on another day
    is a new picture of the account."""
    return f"{as_of.isoformat()}:{holding.row_id}"


def _insert_import(
    state: SqliteState,
    import_id: str,
    target: _Target,
    body: StatementImportRequest,
    reading: _Reading,
    *,
    counts: tuple[int, int, int, int],
    days: tuple[date | None, date | None],
    as_of: date | None,
    now: datetime,
) -> None:
    if reading.mapping is not None:
        recipe = reading.mapping.model_dump_json()
    else:
        recipe = json.dumps(
            {
                "preset": reading.preset.id if reading.preset else None,
                "locale": reading.parsed.locale if reading.parsed else None,
            }
        )
    state.execute(
        "INSERT INTO statement_imports (id, user_id, portfolio_id, connection_id,"
        " filename, mapping_json, rows_total, rows_added, rows_duplicate, rows_skipped,"
        " first_date, last_date, created_at, preset, kind, as_of)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            import_id,
            target.user_id,
            target.portfolio_id,
            target.connection_id,
            body.filename,
            recipe,
            *counts,
            days[0].isoformat() if days[0] else None,
            days[1].isoformat() if days[1] else None,
            _iso(now),
            reading.preset.id if reading.preset else None,
            reading.kind,
            as_of.isoformat() if as_of else None,
        ],
    )


def _preview(
    headers: list[str], reading: _Reading, known: set[str], today: date
) -> StatementPreview:
    parsed = reading.parsed
    preset = reading.preset
    common = {
        "headers": headers,
        "mapping": reading.mapping,
        "guessed": reading.guessed,
        "preset": preset.id if preset else None,
        "preset_label": f"{preset.broker} {preset.label}" if preset else None,
        "locale": parsed.locale if parsed else None,
        "kind": reading.kind,
        "notes": list(parsed.notes) if parsed else [],
    }
    if parsed is not None and parsed.kind == "holdings":
        return _preview_holdings(parsed, reading.as_of or today, known, common)
    rows: list[StatementRowView] = []
    counts = {"new": 0, "duplicate": 0, "skipped": 0}
    days: list[date] = []
    unmapped: set[str] = set()
    names = parsed.unmapped if parsed else {}
    for r in reading.rows:
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
            unmapped.add(_named(a.raw_symbol, names))
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
        **common,
        rows=rows,
        total=len(reading.rows),
        new=counts["new"],
        duplicate=counts["duplicate"],
        skipped=counts["skipped"],
        first_date=min(days) if days else None,
        last_date=max(days) if days else None,
        unmapped=sorted(unmapped),
    )


def _preview_holdings(
    parsed: PresetParse, as_of: date, known: set[str], common: dict[str, Any]
) -> StatementPreview:
    rows: list[StatementRowView] = []
    counts = {"new": 0, "duplicate": 0}
    for h in parsed.holdings:
        status: RowStatus = "duplicate" if _holding_id(as_of, h) in known else "new"
        counts[status] += 1
        rows.append(
            StatementRowView(
                line=h.line,
                status=status,
                kind="cash" if h.is_cash else "holding",
                day=as_of,
                symbol=h.raw_symbol,
                ticker=h.ticker,
                quantity=None if h.is_cash else h.quantity,
                price=None if h.is_cash else h.price,
                amount=h.market_value,
                currency=h.currency,
            )
        )
    rows.extend(StatementRowView(line=r.line, status="skipped", reason=r.skipped)
                for r in parsed.skipped)  # fmt: skip
    rows.sort(key=lambda r: r.line)
    return StatementPreview(
        **common,
        as_of=as_of,
        rows=rows[:PREVIEW_ROWS],
        total=parsed.total,
        new=counts["new"],
        duplicate=counts["duplicate"],
        skipped=len(parsed.skipped),
        first_date=as_of,
        last_date=as_of,
        unmapped=sorted(_named(s, parsed.unmapped) for s in parsed.unmapped),
    )


def _named(symbol: str, names: dict[str, str]) -> str:
    """``US0378331005 (Apple Inc)`` when the export named the product."""
    name = names.get(symbol)
    return f"{symbol} ({name})" if name else symbol


def _rebuild_holdings(state: SqliteState, portfolio_id: str, now: datetime) -> None:
    """One ``sync`` snapshot of what the imports add up to. The latest
    holdings export (not undone) is the starting point on its day; the
    activities dated after it build on it. Without one, every activity
    counts from zero. Each position is its net quantity at its last known
    price, and the cash is what the amounts leave. A CSV portfolio has no
    other snapshots."""
    base = state.sql(
        "SELECT id, as_of FROM statement_imports WHERE portfolio_id = ? AND kind = 'holdings'"
        " AND undone_at IS NULL ORDER BY as_of DESC, created_at DESC, id DESC LIMIT 1",
        [portfolio_id],
    )
    cash = 0.0
    held: dict[str, dict[str, Any]] = {}
    since: str | None = None
    if base:
        since = str(base[0]["as_of"])
        for h in state.sql(
            "SELECT raw_symbol, ticker, quantity, price, currency, is_cash"
            " FROM statement_import_holdings WHERE import_id = ? ORDER BY row_id",
            [base[0]["id"]],
        ):
            if h["is_cash"]:
                cash += float(h["quantity"])
                continue
            pos = held.setdefault(
                h["raw_symbol"], {"ticker": h["ticker"], "qty": 0.0, "price": None, "ccy": None}
            )
            pos["qty"] += float(h["quantity"])
            pos["price"] = float(h["price"]) if h["price"] is not None else pos["price"]
            pos["ccy"] = h["currency"]
    rows = state.sql(
        "SELECT kind, raw_symbol, ticker, quantity, price, amount, currency FROM broker_activities"
        " WHERE portfolio_id = ? AND (? IS NULL OR trade_date > ?) ORDER BY trade_date, id",
        [portfolio_id, since, since],
    )
    state.execute(
        "DELETE FROM portfolio_snapshots WHERE portfolio_id = ? AND source = 'sync'",
        [portfolio_id],
    )
    if not rows and not base:
        return
    for r in rows:
        cash += float(r["amount"] or 0.0)
        if r["kind"] == "trade" and r["raw_symbol"]:
            pos = held.setdefault(
                r["raw_symbol"], {"ticker": r["ticker"], "qty": 0.0, "price": None, "ccy": None}
            )
            pos["qty"] += float(r["quantity"] or 0.0)
            pos["ticker"] = pos["ticker"] or r["ticker"]
            if r["price"] is not None:
                pos["price"] = float(r["price"])
                pos["ccy"] = r["currency"]
    open_positions = {s: p for s, p in held.items() if abs(float(p["qty"])) > 1e-9}
    value = sum(float(p["qty"]) * float(p["price"] or 0.0) for p in open_positions.values())
    mapped = {str(p["ticker"]): float(p["qty"]) for p in open_positions.values() if p["ticker"]}
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
        qty = float(p["qty"])
        price = float(p["price"]) if p["price"] is not None else None
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
