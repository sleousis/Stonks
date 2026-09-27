"""Connections service: connect, link, sync and disconnect broker
connections, every call scoped by an :class:`~stonks.accounts.scope.Scope`.

Rules (design section 6):

- A connection belongs to one person. Another user's connection is "not
  found" (never "forbidden"); service scopes (scheduler, system) reach every
  connection but cannot own one.
- A provider works only when enabled in ``[connections].enabled_providers``
  and configured; a disabled provider is refused everywhere, including
  syncs of connections made while it was enabled.
- Credentials are sealed with :class:`~stonks.security.SecretBox`, bound to
  their connection id, and opened only to talk to the provider. Nothing that
  leaves this module (results, errors, audit rows, logs) contains them.
- A sync is read-only at the provider and idempotent here: one
  ``portfolio_snapshots`` row per portfolio and day (``source = 'sync'``,
  updated in place), holdings in ``broker_positions`` (unmapped symbols kept
  with ``ticker = NULL``), activities upserted by provider id.
- Connect, link, disconnect, sync and credential rotation each write an
  ``audit_log`` row.

Threads: a scheduled pass (:meth:`ConnectionService.sync_due`) fetches from
providers on a thread pool (I/O only) and writes to SQLite on the calling
thread, one transaction per connection.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import secrets
import sqlite3
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from stonks.accounts.audit import AuditLog
from stonks.accounts.models import DEFAULT_PORTFOLIO_ID, NotFound
from stonks.accounts.portfolios import PortfolioRepository
from stonks.accounts.scope import Scope, owned_portfolio
from stonks.connections.base import (
    AccountBalances,
    Activity,
    BrokerConnection,
    Capability,
    ConnectionsError,
    Credentials,
    ExternalAccount,
    ExternalPosition,
    PortalFlow,
    ProviderAuthError,
    ProviderContext,
    ProviderError,
    RateLimited,
)
from stonks.connections.models import (
    BrokerAccountRow,
    ConnectionRecord,
    DisconnectResult,
    ImportEnvResult,
    PortalLink,
    PortfolioSyncResult,
    ProviderInfo,
    SyncResult,
)
from stonks.connections.ratelimit import limiter_for
from stonks.connections.registry import enabled_provider, provider_classes
from stonks.connections.settings import ConnectionsConfig
from stonks.logging import get_logger
from stonks.security import SecretBox, SecretBoxError
from stonks.store.state import SqliteState

_log = get_logger("stonks.connections")

_MARKET_TZ = ZoneInfo("America/New_York")
_MARKET_OPEN, _MARKET_CLOSE = time(9, 30), time(16, 0)
_MAX_BACKOFF = timedelta(hours=6)
_ACTIVITY_OVERLAP = timedelta(days=7)
_MAX_ERROR = 300
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat(timespec="seconds")


def trading_date(ts: datetime) -> date:
    """The US trading date a wall-clock instant belongs to."""
    return ts.astimezone(_MARKET_TZ).date()


def next_sync_after(ts: datetime, config: ConnectionsConfig) -> datetime:
    """Every ``market_hours_sync_minutes`` during US market hours, every
    ``off_hours_sync_minutes`` otherwise."""
    local = ts.astimezone(_MARKET_TZ)
    open_now = local.weekday() < 5 and _MARKET_OPEN <= local.time() < _MARKET_CLOSE
    minutes = config.market_hours_sync_minutes if open_now else config.off_hours_sync_minutes
    return ts + timedelta(minutes=minutes)


def backoff(failures: int, retry_after: float | None = None) -> timedelta:
    """1, 2, 4, ... minutes, at least ``retry_after``, at most six hours."""
    delay = timedelta(minutes=2 ** max(0, failures - 1))
    if retry_after:
        delay = max(delay, timedelta(seconds=retry_after))
    return min(delay, _MAX_BACKOFF)


# ---- fetch phase (I/O only, thread-safe) ---------------------------------------------


@dataclass
class _Target:
    portfolio_id: str
    account_id: str
    since: date


@dataclass
class _PortfolioFetch:
    target: _Target
    balances: AccountBalances | None = None
    positions: list[ExternalPosition] = field(default_factory=list)
    activities: list[Activity] = field(default_factory=list)
    error: str | None = None


@dataclass
class _Fetch:
    record: ConnectionRecord
    accounts: list[ExternalAccount] = field(default_factory=list)
    portfolios: list[_PortfolioFetch] = field(default_factory=list)
    error: ProviderError | ConnectionsError | None = None


@dataclass
class _Job:
    record: ConnectionRecord
    cls: type[BrokerConnection]
    credentials: Credentials | None
    targets: list[_Target]
    error: ConnectionsError | None = None


class ConnectionService:
    def __init__(
        self,
        state: SqliteState,
        config: ConnectionsConfig,
        *,
        box: SecretBox | None = None,
        clock: Callable[[], datetime] = _now,
        transports: Mapping[str, Any] | None = None,
    ) -> None:
        """``box`` = None: loaded from the environment on first use (reads
        such as :meth:`list` never need it). ``transports`` maps a provider
        name to a vendor transport (tests only)."""
        self._state = state
        self.config = config
        self._box = box
        self._clock = clock
        self._transports = dict(transports or {})
        self._audit = AuditLog(state)
        self._portfolios = PortfolioRepository(state)

    # ---- providers and reads -------------------------------------------------------

    def available_providers(self, scope: Scope) -> list[ProviderInfo]:
        """Providers a user may connect: enabled by an admin and configured."""
        return [p for p in self.providers(scope) if p.enabled]

    def providers(self, scope: Scope) -> list[ProviderInfo]:
        """Every registered provider, each marked ``enabled`` when an admin
        turned it on and it is configured."""
        out: list[ProviderInfo] = []
        for name, cls in provider_classes().items():
            try:
                enabled_provider(self.config, name)
            except ConnectionsError:
                enabled = False
            else:
                enabled = True
            out.append(
                ProviderInfo(
                    name=name,
                    display_name=cls.display_name,
                    auth_flow=cls.auth_flow,
                    capabilities=tuple(sorted(c.value for c in cls.capabilities)),
                    credential_fields=tuple(cls.credential_fields),
                    can_trade=cls.supports(Capability.TRADE),
                    enabled=enabled,
                    has_paper=cls.has_paper,
                )
            )
        return out

    def account_counts(self, connection_ids: list[str]) -> dict[str, int]:
        """Broker accounts seen per connection (0 when none). The ids must
        already be scope-checked by the caller."""
        if not connection_ids:
            return {}
        marks = ", ".join("?" for _ in connection_ids)
        rows = self._state.sql(
            "SELECT connection_id, COUNT(*) AS n FROM broker_accounts"
            f" WHERE connection_id IN ({marks}) GROUP BY connection_id",
            list(connection_ids),
        )
        counts = {r["connection_id"]: int(r["n"]) for r in rows}
        return {cid: counts.get(cid, 0) for cid in connection_ids}

    def list(self, scope: Scope) -> list[ConnectionRecord]:
        if scope.is_service:
            rows = self._state.sql("SELECT * FROM broker_connections ORDER BY created_at, id")
        else:
            rows = self._state.sql(
                "SELECT c.* FROM broker_connections c JOIN users u ON u.id = c.user_id"
                " WHERE c.user_id = ? AND u.status = 'active' ORDER BY c.created_at, c.id",
                [scope.user_id],
            )
        return [ConnectionRecord.from_row(r) for r in rows]

    def get(self, scope: Scope, connection_id: str) -> ConnectionRecord:
        return owned_connection(self._state, scope, connection_id)

    def accounts(self, scope: Scope, connection_id: str) -> list[BrokerAccountRow]:
        owned_connection(self._state, scope, connection_id)
        rows = self._state.sql(
            "SELECT a.*, p.id AS portfolio_id FROM broker_accounts a"
            " LEFT JOIN portfolios p ON p.broker_connection_id = a.connection_id"
            "  AND p.external_account_id = a.external_account_id"
            " WHERE a.connection_id = ? ORDER BY a.first_seen_at, a.external_account_id",
            [connection_id],
        )
        return [
            BrokerAccountRow(
                connection_id=r["connection_id"],
                external_account_id=r["external_account_id"],
                name=r["name"],
                institution=r["institution"],
                number_mask=r["number_mask"],
                currency=r["currency"],
                portfolio_id=r["portfolio_id"],
            )
            for r in rows
        ]

    # ---- connect: API keys --------------------------------------------------------------

    def connect_with_keys(
        self,
        scope: Scope,
        provider: str,
        fields: Mapping[str, str],
        *,
        label: str | None = None,
        link_accounts: bool = True,
    ) -> ConnectionRecord:
        """Validate the keys against the provider (listing accounts), then
        store them sealed. Refused keys are never stored."""
        _require_person(scope)
        cls = enabled_provider(self.config, provider)
        if cls.auth_flow != "api_key":
            raise ConnectionsError(f"{provider} connects through its hosted portal, not API keys")
        credentials = _key_credentials(cls, fields)
        connection_id = _new_id()
        accounts = self._with_provider(cls, credentials, connection_id, lambda c: c.accounts())
        box = self._secret_box()
        now = self._clock()
        with self._state.transaction():
            self._insert_connection(scope, connection_id, provider, label, "active", None, now)
            self._store_credentials(box, connection_id, credentials, now)
            self._upsert_accounts(connection_id, accounts, now)
            self._audit.record(
                scope.actor,
                "connection.connect",
                "broker_connection",
                connection_id,
                details={"provider": provider, "flow": "api_key", "accounts": len(accounts)},
            )
            if link_accounts:
                self._link_new_accounts(scope, connection_id, provider, accounts)
        _log.info("connections.connected", connection_id=connection_id, provider=provider,
                  user_id=scope.user_id, accounts=len(accounts))  # fmt: skip
        return self.get(scope, connection_id)

    # ---- connect: hosted portal (OAuth / aggregator) -----------------------------------

    def start_portal(
        self,
        scope: Scope,
        provider: str,
        redirect_uri: str,
        *,
        connection_id: str | None = None,
        label: str | None = None,
    ) -> PortalLink:
        """Register the provider-side user (first time) and return the
        one-time portal URL. The callback URL gets ``connection_id`` and a
        random ``state`` appended; only its SHA-256 is stored."""
        _require_person(scope)
        cls = enabled_provider(self.config, provider)
        if not issubclass(cls, PortalFlow):
            raise ConnectionsError(f"{provider} connects with API keys, not a hosted portal")
        _check_redirect(redirect_uri)
        box = self._secret_box()
        now = self._clock()
        if connection_id is None:
            connection_id = _new_id()
            external_user_id = f"stonks-{connection_id}"
            context = self._context(cls, connection_id)
            credentials = cls.register_user(context, external_user_id)
            with self._state.transaction():
                self._insert_connection(
                    scope, connection_id, provider, label, "pending", external_user_id, now
                )
                self._store_credentials(box, connection_id, credentials, now)
        else:
            record = owned_connection(self._state, scope, connection_id)
            if record.provider != provider or not record.external_user_id:
                raise ConnectionsError("that connection can't be re-linked through this provider")
            external_user_id = record.external_user_id
            credentials = self._open_credentials(box, connection_id)
            context = self._context(cls, connection_id)
        state_token = secrets.token_urlsafe(32)
        callback = _with_query(redirect_uri, connection_id=connection_id, state=state_token)
        try:
            url = cls.portal_url(context, external_user_id, credentials, callback)
        except ProviderError as exc:
            raise _redacted(exc, credentials) from None
        expires = now + timedelta(minutes=self.config.portal_ttl_minutes)
        with self._state.transaction():
            self._state.execute(
                "UPDATE broker_connections SET pending_state_hash = ?, pending_expires_at = ?,"
                " updated_at = ? WHERE id = ?",
                [_hash_state(state_token), _iso(expires), _iso(now), connection_id],
            )
            self._audit.record(
                scope.actor,
                "connection.connect_start",
                "broker_connection",
                connection_id,
                details={"provider": provider, "flow": "portal"},
            )
        return PortalLink(connection_id=connection_id, url=url, expires_at=_iso(expires))

    def complete_portal(
        self,
        scope: Scope,
        connection_id: str,
        state: str,
        *,
        outcome: str | None = None,
        link_accounts: bool = True,
    ) -> ConnectionRecord:
        """The callback: check the one-time ``state``, then list the
        accounts the user linked. ``outcome`` is the provider's status
        parameter, if it sends one (anything but success keeps the
        connection pending)."""
        record = owned_connection(self._state, scope, connection_id)
        row = self._state.sql(
            "SELECT pending_state_hash, pending_expires_at FROM broker_connections WHERE id = ?",
            [connection_id],
        )[0]
        stored, expires = row["pending_state_hash"], row["pending_expires_at"]
        if not stored or not hmac.compare_digest(stored, _hash_state(state or "")):
            raise ConnectionsError("invalid callback state; start connecting again")
        now = self._clock()
        if expires is None or _iso(now) > expires:
            self._clear_state(connection_id, now)
            raise ConnectionsError("the connection link expired; start connecting again")
        if outcome is not None and outcome.strip().upper() not in ("SUCCESS", "OK", "CONNECTED"):
            with self._state.transaction():
                self._state.execute(
                    "UPDATE broker_connections SET pending_state_hash = NULL,"
                    " pending_expires_at = NULL, last_error = ?, updated_at = ? WHERE id = ?",
                    ["the connection was not completed at the provider", _iso(now), connection_id],
                )
                self._audit.record(
                    scope.actor, "connection.connect_abandoned", "broker_connection",
                    connection_id, details={"provider": record.provider},
                )  # fmt: skip
            return self.get(scope, connection_id)
        cls = enabled_provider(self.config, record.provider)
        box = self._secret_box()
        credentials = self._open_credentials(box, connection_id)
        accounts = self._with_provider(cls, credentials, connection_id, lambda c: c.accounts())
        with self._state.transaction():
            self._state.execute(
                "UPDATE broker_connections SET status = 'active', pending_state_hash = NULL,"
                " pending_expires_at = NULL, last_error = NULL, updated_at = ? WHERE id = ?",
                [_iso(now), connection_id],
            )
            self._upsert_accounts(connection_id, accounts, now)
            self._audit.record(
                scope.actor,
                "connection.connect",
                "broker_connection",
                connection_id,
                details={"provider": record.provider, "flow": "portal", "accounts": len(accounts)},
            )
            if link_accounts:
                self._link_new_accounts(scope, connection_id, record.provider, accounts)
        return self.get(scope, connection_id)

    # ---- linking --------------------------------------------------------------------------

    def link_account(
        self,
        scope: Scope,
        connection_id: str,
        external_account_id: str,
        *,
        portfolio_id: str | None = None,
        name: str | None = None,
    ) -> str:
        """Link one external account to a broker-kind portfolio of the same
        user (a new one unless ``portfolio_id`` is given). Returns its id."""
        record = owned_connection(self._state, scope, connection_id)
        with self._state.transaction():
            return self._link(scope, record, external_account_id, portfolio_id, name)

    def _link(
        self,
        scope: Scope,
        record: ConnectionRecord,
        external_account_id: str,
        portfolio_id: str | None,
        name: str | None,
    ) -> str:
        if scope.is_service:
            raise ConnectionsError("accounts are linked by their owner")
        acc = self._state.sql(
            "SELECT * FROM broker_accounts WHERE connection_id = ? AND external_account_id = ?",
            [record.id, external_account_id],
        )
        if not acc:
            raise NotFound(f"account {external_account_id!r} not found on this connection")
        if portfolio_id is None:
            portfolio = self._portfolios.create(
                scope,
                name=self._free_name(scope, name or f"{record.provider}: {acc[0]['name']}"),
                kind="broker",
                base_currency=acc[0]["currency"],
            )
        else:
            portfolio = owned_portfolio(self._state, scope, portfolio_id)
            if portfolio.kind != "broker":
                raise ConnectionsError("only broker portfolios can mirror a broker account")
            if portfolio.broker_connection_id not in (None, record.id):
                raise ConnectionsError("that portfolio is linked to another connection")
        try:
            self._state.execute(
                "UPDATE portfolios SET broker_connection_id = ?, external_account_id = ?,"
                " status = CASE WHEN status = 'archived' THEN 'active' ELSE status END"
                " WHERE id = ?",
                [record.id, external_account_id, portfolio.id],
            )
        except sqlite3.IntegrityError:
            raise ConnectionsError("that account is already linked to a portfolio") from None
        self._audit.record(
            scope.actor,
            "connection.link",
            "broker_connection",
            record.id,
            portfolio_id=portfolio.id,
            details={"external_account_id": external_account_id},
        )
        return portfolio.id

    def _link_new_accounts(
        self, scope: Scope, connection_id: str, provider: str, accounts: list[ExternalAccount]
    ) -> None:
        record = owned_connection(self._state, scope, connection_id)
        linked = {
            r[0]
            for r in self._state.sql(
                "SELECT external_account_id FROM portfolios WHERE broker_connection_id = ?",
                [connection_id],
            )
        }
        for account in accounts:
            if account.id not in linked:
                self._link(scope, record, account.id, None, None)

    def _free_name(self, scope: Scope, base: str) -> str:
        taken = {p.name for p in self._portfolios.list(scope)}
        name, n = base, 2
        while name in taken:
            name, n = f"{base} ({n})", n + 1
        return name

    # ---- disconnect -------------------------------------------------------------------------

    def disconnect(self, scope: Scope, connection_id: str) -> DisconnectResult:
        """Remove the connection, its credentials, accounts and activities.
        Linked portfolios are unlinked and archived (their snapshots stay);
        their auto subscriptions pause. A hosted-login provider's remote user
        is removed first, best effort."""
        record = owned_connection(self._state, scope, connection_id)
        remote_removed: bool | None = None
        remote_error: str | None = None
        cls = provider_classes().get(record.provider)
        if cls is not None and issubclass(cls, PortalFlow) and record.external_user_id:
            try:
                credentials = self._open_credentials(self._secret_box(), connection_id)
                cls.unregister_user(
                    self._context(cls, connection_id), record.external_user_id, credentials
                )
                remote_removed = True
            except (ConnectionsError, SecretBoxError) as exc:
                remote_removed = False
                remote_error = _short(str(exc))
            except Exception as exc:  # a provider bug must not block removal
                remote_removed = False
                remote_error = _unexpected(record.provider, "remove the provider-side user", exc)
            if remote_error:
                _log.warning("connections.remote_remove_failed", connection_id=connection_id,
                             provider=record.provider, error=remote_error)  # fmt: skip
        now = self._clock()
        with self._state.transaction():
            linked = [
                r[0]
                for r in self._state.sql(
                    "SELECT id FROM portfolios WHERE broker_connection_id = ? ORDER BY id",
                    [connection_id],
                )
            ]
            for pid in linked:
                self._state.execute(
                    "UPDATE portfolios SET broker_connection_id = NULL,"
                    " external_account_id = NULL, status = 'archived' WHERE id = ?",
                    [pid],
                )
            self._pause_auto(linked, "connection_removed", now)
            self._state.execute("DELETE FROM broker_connections WHERE id = ?", [connection_id])
            self._audit.record(
                scope.actor,
                "connection.disconnect",
                "broker_connection",
                connection_id,
                details={
                    "provider": record.provider,
                    "archived_portfolios": linked,
                    "remote_removed": remote_removed,
                },
            )
        return DisconnectResult(
            connection_id=connection_id,
            archived_portfolios=tuple(linked),
            remote_removed=remote_removed,
            remote_error=remote_error,
        )

    # ---- sync ------------------------------------------------------------------------------

    def sync(self, scope: Scope, connection_id: str) -> SyncResult:
        """Sync one connection now (manual, or a retry after an error)."""
        record = owned_connection(self._state, scope, connection_id)
        if record.status == "pending":
            raise ConnectionsError("finish connecting before syncing")
        cls = enabled_provider(self.config, record.provider)
        job = self._job(record, cls)
        return self._apply(scope, _fetch(job, self._context(cls, record.id)))

    def sync_due(self, scope: Scope, *, max_workers: int | None = None) -> list[SyncResult]:
        """One scheduled pass: every active connection whose next sync is
        due, of enabled providers only. Service scopes only."""
        if not scope.is_service:
            raise ConnectionsError("scheduled syncs run as a service principal")
        now = _iso(self._clock())
        rows = self._state.sql(
            "SELECT c.* FROM broker_connections c JOIN users u ON u.id = c.user_id"
            " WHERE c.status = 'active' AND u.status = 'active'"
            " AND (c.next_sync_at IS NULL OR c.next_sync_at <= ?) ORDER BY c.next_sync_at, c.id",
            [now],
        )
        jobs: list[tuple[_Job, ProviderContext]] = []
        for row in rows:
            record = ConnectionRecord.from_row(row)
            try:
                cls = enabled_provider(self.config, record.provider)
            except ConnectionsError as exc:
                _log.warning("connections.sync_skipped", connection_id=record.id,
                             provider=record.provider, reason=str(exc))  # fmt: skip
                continue
            jobs.append((self._job(record, cls), self._context(cls, record.id)))
        if not jobs:
            return []
        workers = max(1, min(max_workers or self.config.sync_workers, len(jobs)))
        if workers == 1:
            fetched = [_fetch(job, ctx) for job, ctx in jobs]
        else:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sync") as pool:
                fetched = list(pool.map(lambda jc: _fetch(*jc), jobs))
        return [self._apply(scope, f) for f in fetched]

    def _job(self, record: ConnectionRecord, cls: type[BrokerConnection]) -> _Job:
        targets = [
            _Target(r["id"], r["external_account_id"], self._activity_since(record.id, r["id"]))
            for r in self._state.sql(
                "SELECT id, external_account_id FROM portfolios WHERE broker_connection_id = ?"
                " AND external_account_id IS NOT NULL AND status <> 'archived' ORDER BY id",
                [record.id],
            )
        ]
        try:
            credentials = self._open_credentials(self._secret_box(), record.id)
        except (ConnectionsError, SecretBoxError) as exc:
            return _Job(record, cls, None, targets, ConnectionsError(f"credentials: {exc}"))
        return _Job(record, cls, credentials, targets)

    def _activity_since(self, connection_id: str, portfolio_id: str) -> date:
        last = self._state.sql(
            "SELECT MAX(trade_date) FROM broker_activities WHERE connection_id = ?"
            " AND portfolio_id = ?",
            [connection_id, portfolio_id],
        )[0][0]
        today = trading_date(self._clock())
        if last:
            return min(date.fromisoformat(last[:10]), today) - _ACTIVITY_OVERLAP
        return today - timedelta(days=self.config.activity_lookback_days)

    def _apply(self, scope: Scope, fetched: _Fetch) -> SyncResult:
        record = fetched.record
        now = self._clock()
        if fetched.error is not None:
            return self._record_failure(scope, record, fetched.error, now)
        as_of = trading_date(now)
        results: list[PortfolioSyncResult] = []
        with self._state.transaction():
            self._upsert_accounts(record.id, fetched.accounts, now)
            for pf in fetched.portfolios:
                if pf.error is not None:
                    results.append(
                        PortfolioSyncResult(
                            pf.target.portfolio_id, pf.target.account_id, error=pf.error
                        )
                    )
                    continue
                results.append(self._write_portfolio(record.id, pf, as_of, now))
            status = "partial" if any(r.error for r in results) else "ok"
            partial_error = "; ".join(r.error for r in results if r.error) or None
            next_at = next_sync_after(now, self.config)
            self._state.execute(
                "UPDATE broker_connections SET status = 'active', last_sync_at = ?,"
                " last_sync_status = ?, last_error = ?, consecutive_failures = 0,"
                " next_sync_at = ?, updated_at = ? WHERE id = ?",
                [_iso(now), status, partial_error, _iso(next_at), _iso(now), record.id],
            )
            self._audit.record(
                scope.actor,
                "connection.sync",
                "broker_connection",
                record.id,
                details={
                    "status": status,
                    "as_of": as_of.isoformat(),
                    "portfolios": [r.portfolio_id for r in results],
                    "positions": sum(r.positions for r in results),
                    "unmapped": sorted({s for r in results for s in r.unmapped}),
                    "activities_new": sum(r.activities_new for r in results),
                },
            )
        _log.info("connections.synced", connection_id=record.id, provider=record.provider,
                  status=status, portfolios=len(results))  # fmt: skip
        return SyncResult(
            connection_id=record.id,
            status=status,
            portfolios=tuple(results),
            error=partial_error,
            next_sync_at=_iso(next_at),
            accounts_seen=len(fetched.accounts),
        )

    def _write_portfolio(
        self, connection_id: str, pf: _PortfolioFetch, as_of: date, now: datetime
    ) -> PortfolioSyncResult:
        assert pf.balances is not None
        mapped: dict[str, float] = {}
        for p in pf.positions:
            if p.ticker is not None:
                mapped[p.ticker] = mapped.get(p.ticker, 0.0) + p.quantity
        total = pf.balances.total_value
        if total is None:
            total = pf.balances.cash + sum(p.value or 0.0 for p in pf.positions)
        pid = pf.target.portfolio_id
        self._state.execute(
            "INSERT INTO portfolio_snapshots (tick_id, as_of, taken_at, cash, positions_json,"
            " total_value, portfolio_id, source) VALUES (NULL, ?, ?, ?, ?, ?, ?, 'sync')"
            " ON CONFLICT (portfolio_id, as_of) WHERE source = 'sync' DO UPDATE SET"
            " taken_at = excluded.taken_at, cash = excluded.cash,"
            " positions_json = excluded.positions_json, total_value = excluded.total_value",
            [as_of.isoformat(), _iso(now), pf.balances.cash,
             json.dumps(mapped, sort_keys=True), float(total), pid],
        )  # fmt: skip
        snapshot_id = int(
            self._state.sql(
                "SELECT id FROM portfolio_snapshots WHERE portfolio_id = ? AND as_of = ?"
                " AND source = 'sync'",
                [pid, as_of.isoformat()],
            )[0][0]
        )
        self._state.execute("DELETE FROM broker_positions WHERE snapshot_id = ?", [snapshot_id])
        merged: dict[str, ExternalPosition] = {}
        for p in pf.positions:
            prev = merged.get(p.raw_symbol)
            if prev is not None:  # same symbol twice (lots): one row, summed
                p = ExternalPosition(
                    raw_symbol=p.raw_symbol, ticker=p.ticker,
                    quantity=prev.quantity + p.quantity, price=p.price,
                    market_value=(prev.value or 0.0) + (p.value or 0.0),
                    currency=p.currency, description=p.description,
                )  # fmt: skip
            merged[p.raw_symbol] = p
        for p in merged.values():
            self._state.execute(
                "INSERT INTO broker_positions (snapshot_id, portfolio_id, raw_symbol, ticker,"
                " quantity, price, market_value, currency, description)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [snapshot_id, pid, p.raw_symbol, p.ticker, p.quantity, p.price, p.value,
                 p.currency, p.description],
            )  # fmt: skip
        known = {
            r[0]
            for r in self._state.sql(
                "SELECT provider_activity_id FROM broker_activities WHERE connection_id = ?",
                [connection_id],
            )
        }
        new = 0
        for a in pf.activities:
            new += a.provider_activity_id not in known
            known.add(a.provider_activity_id)
            self._state.execute(
                "INSERT INTO broker_activities (connection_id, portfolio_id, external_account_id,"
                " provider_activity_id, kind, raw_symbol, ticker, quantity, price, amount, fee,"
                " currency, trade_date, settle_date, description, synced_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (connection_id, provider_activity_id) DO UPDATE SET"
                " portfolio_id = excluded.portfolio_id, kind = excluded.kind,"
                " raw_symbol = excluded.raw_symbol, ticker = excluded.ticker,"
                " quantity = excluded.quantity, price = excluded.price,"
                " amount = excluded.amount, fee = excluded.fee, currency = excluded.currency,"
                " trade_date = excluded.trade_date, settle_date = excluded.settle_date,"
                " description = excluded.description, synced_at = excluded.synced_at",
                [connection_id, pid, pf.target.account_id, a.provider_activity_id, a.kind,
                 a.raw_symbol, a.ticker, a.quantity, a.price, a.amount, a.fee, a.currency,
                 a.trade_date.isoformat() if a.trade_date else None,
                 a.settle_date.isoformat() if a.settle_date else None,
                 a.description, _iso(now)],
            )  # fmt: skip
        return PortfolioSyncResult(
            portfolio_id=pid,
            external_account_id=pf.target.account_id,
            snapshot_id=snapshot_id,
            positions=len(merged),
            unmapped=tuple(sorted(p.raw_symbol for p in merged.values() if p.ticker is None)),
            activities_new=new,
            activities_seen=len(pf.activities),
        )

    def _record_failure(
        self,
        scope: Scope,
        record: ConnectionRecord,
        error: ProviderError | ConnectionsError,
        now: datetime,
    ) -> SyncResult:
        failures = record.consecutive_failures + 1
        message = _short(str(error))
        auth = isinstance(error, ProviderAuthError)
        retry_after = error.retry_after if isinstance(error, RateLimited) else None
        next_at = None if auth else _iso(now + backoff(failures, retry_after))
        with self._state.transaction():
            self._state.execute(
                "UPDATE broker_connections SET status = ?, last_sync_status = 'error',"
                " last_error = ?, consecutive_failures = ?, next_sync_at = ?, updated_at = ?"
                " WHERE id = ?",
                ["error" if auth else record.status, message, failures, next_at, _iso(now),
                 record.id],
            )  # fmt: skip
            paused = 0
            if auth:
                linked = [
                    r[0]
                    for r in self._state.sql(
                        "SELECT id FROM portfolios WHERE broker_connection_id = ?", [record.id]
                    )
                ]
                paused = self._pause_auto(linked, "broker_error", now)
            self._audit.record(
                scope.actor,
                "connection.sync_failed",
                "broker_connection",
                record.id,
                details={
                    "error": message,
                    "auth": auth,
                    "failures": failures,
                    "auto_paused": paused,
                },
            )
        _log.warning("connections.sync_failed", connection_id=record.id, provider=record.provider,
                     error=message, auth=auth, failures=failures)  # fmt: skip
        return SyncResult(
            connection_id=record.id, status="error", error=message, next_sync_at=next_at
        )

    def _pause_auto(self, portfolio_ids: list[str], reason: str, now: datetime) -> int:
        if not portfolio_ids:
            return 0
        marks = ",".join("?" for _ in portfolio_ids)
        cur = self._state.execute(
            "UPDATE subscriptions SET paused_reason = ?, updated_at = ?"
            f" WHERE mode = 'auto' AND paused_reason IS NULL AND portfolio_id IN ({marks})",
            [reason, _iso(now), *portfolio_ids],
        )
        return int(cur.rowcount or 0)

    # ---- trading (auto mode, S6) -----------------------------------------------------------

    def open_trader(self, scope: Scope, portfolio_id: str) -> Any:
        """The ``Broker`` placing orders in the account ``portfolio_id``
        mirrors, through its connection's trading adapter. Refused when the
        portfolio isn't linked, the provider is disabled or can't trade.
        Provider errors come back redacted."""
        from stonks.connections.base import Capability

        portfolio = owned_portfolio(self._state, scope, portfolio_id)
        if not portfolio.broker_connection_id or not portfolio.external_account_id:
            raise ConnectionsError(f"portfolio {portfolio_id!r} is not linked to a broker account")
        record = owned_connection(self._state, scope, portfolio.broker_connection_id)
        cls = enabled_provider(self.config, record.provider)
        cls.require(Capability.TRADE)
        credentials = self._open_credentials(self._secret_box(), record.id)
        try:
            # the trader runs in the caller's thread, so it may use the state
            # DB (the IBKR contract cache and orderRef lookup, roadmap 19.3)
            context = self._context(cls, record.id, extra={"state": self._state})
            conn = cls.open(credentials, context)
            return conn.trader(portfolio.external_account_id)
        except ProviderError as exc:
            raise _redacted(exc, credentials) from None

    # ---- key rotation ---------------------------------------------------------------------

    def rotate_credentials(self, scope: Scope) -> int:
        """Re-seal every credential not sealed under the active master key.
        Service scopes only (CLI / scheduler). Returns how many changed."""
        if not scope.is_service:
            raise ConnectionsError("credential rotation runs as a service principal")
        box = self._secret_box()
        rows = self._state.sql(
            "SELECT connection_id, ciphertext FROM broker_credentials WHERE key_id <> ?",
            [box.active_key_id],
        )
        now = _iso(self._clock())
        with self._state.transaction():
            for row in rows:
                fresh = box.rotate(_parse_sealed(row["ciphertext"]), aad=_aad(row["connection_id"]))
                self._state.execute(
                    "UPDATE broker_credentials SET key_id = ?, ciphertext = ?, rotated_at = ?"
                    " WHERE connection_id = ?",
                    [fresh.key_id, fresh.token, now, row["connection_id"]],
                )
            if rows:
                self._audit.record(
                    scope.actor,
                    "connection.credentials_rotated",
                    "broker_credentials",
                    None,
                    details={"count": len(rows), "key_id": box.active_key_id},
                )
        return len(rows)

    # ---- migration of the single-owner Alpaca install -------------------------------------

    def import_env(self, scope: Scope, settings: Any) -> ImportEnvResult:
        """Turn today's env-configured Alpaca account (``[brokers].kind =
        "alpaca"`` with ``ALPACA_API_KEY`` / ``ALPACA_SECRET_KEY``) into a
        connection of ``pf_default``'s owner, make ``pf_default`` a broker
        portfolio mirroring it, and switch its paper subscriptions to auto,
        because that is what the tick already does with them. Idempotent."""
        brokers = settings.brokers
        if brokers.kind != "alpaca":
            return ImportEnvResult(
                "not_applicable", "[brokers].kind is not alpaca; nothing to import"
            )
        portfolio = owned_portfolio(self._state, scope, DEFAULT_PORTFOLIO_ID)
        if portfolio.broker_connection_id:
            return ImportEnvResult(
                "already_imported",
                f"{DEFAULT_PORTFOLIO_ID} already mirrors connection {portfolio.broker_connection_id}",
                connection_id=portfolio.broker_connection_id,
                portfolio_id=portfolio.id,
                external_account_id=portfolio.external_account_id,
            )
        alpaca = brokers.alpaca
        if not alpaca.api_key or not alpaca.secret_key:
            raise ConnectionsError("set ALPACA_API_KEY and ALPACA_SECRET_KEY to import them")
        fields = {
            "api_key": alpaca.api_key.get_secret_value(),
            "secret_key": alpaca.secret_key.get_secret_value(),
            "paper": "true" if alpaca.paper else "false",
        }
        record = self.connect_with_keys(
            scope, "alpaca", fields, label="Imported from env", link_accounts=False
        )
        found = self.accounts(scope, record.id)
        if not found:
            raise ConnectionsError("the Alpaca key returned no account")
        account = found[0]
        now = self._clock()
        switched: list[str] = []
        with self._state.transaction():
            self._state.execute(
                "UPDATE portfolios SET kind = 'broker' WHERE id = ?", [DEFAULT_PORTFOLIO_ID]
            )
            self._link(scope, record, account.external_account_id, DEFAULT_PORTFOLIO_ID, None)
            for row in self._state.sql(
                "SELECT id FROM subscriptions WHERE portfolio_id = ? AND mode = 'paper'"
                " ORDER BY id",
                [DEFAULT_PORTFOLIO_ID],
            ):
                self._state.execute(
                    "UPDATE subscriptions SET mode = 'auto', auto_enabled_at = ?,"
                    " auto_enabled_by = ?, paused_reason = NULL, updated_at = ? WHERE id = ?",
                    [_iso(now), scope.actor, _iso(now), row["id"]],
                )
                self._audit.record(
                    scope.actor,
                    "subscription.mode",
                    "subscription",
                    row["id"],
                    portfolio_id=DEFAULT_PORTFOLIO_ID,
                    details={
                        "from": "paper",
                        "to": "auto",
                        "reason": "import-env: [brokers].kind = alpaca already trades this book",
                    },
                )
                switched.append(row["id"])
            self._audit.record(
                scope.actor,
                "connection.import_env",
                "broker_connection",
                record.id,
                portfolio_id=DEFAULT_PORTFOLIO_ID,
                details={"subscriptions_switched": switched, "paper": alpaca.paper},
            )
        return ImportEnvResult(
            "imported",
            f"{DEFAULT_PORTFOLIO_ID} now mirrors Alpaca account {account.external_account_id}",
            connection_id=record.id,
            portfolio_id=DEFAULT_PORTFOLIO_ID,
            external_account_id=account.external_account_id,
            subscriptions_switched=tuple(switched),
        )

    # ---- internals ---------------------------------------------------------------------

    def _secret_box(self) -> SecretBox:
        if self._box is None:
            self._box = SecretBox.from_env()
        return self._box

    def _context(
        self,
        cls: type[BrokerConnection],
        connection_id: str,
        *,
        extra: dict[str, Any] | None = None,
    ) -> ProviderContext:
        return ProviderContext(
            config=self.config,
            connection_id=connection_id,
            limiter=limiter_for(cls.provider, cls.rate_limit),
            transport=self._transports.get(cls.provider),
            extra=dict(extra or {}),
        )

    def _with_provider(
        self,
        cls: type[BrokerConnection],
        credentials: Credentials,
        connection_id: str,
        fn: Callable[[BrokerConnection], Any],
    ) -> Any:
        try:
            conn = cls.open(credentials, self._context(cls, connection_id))
            try:
                return fn(conn)
            finally:
                conn.close()
        except ProviderError as exc:
            raise _redacted(exc, credentials) from None
        except ConnectionsError as exc:
            raise ConnectionsError(credentials.redact(str(exc))) from None
        except Exception as exc:
            raise ConnectionsError(_unexpected(cls.provider, "connect", exc)) from None

    def _insert_connection(
        self,
        scope: Scope,
        connection_id: str,
        provider: str,
        label: str | None,
        status: str,
        external_user_id: str | None,
        now: datetime,
    ) -> None:
        self._state.execute(
            "INSERT INTO broker_connections (id, user_id, provider, label, status,"
            " external_user_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [connection_id, scope.user_id, provider, label, status, external_user_id,
             _iso(now), _iso(now)],
        )  # fmt: skip

    def _store_credentials(
        self, box: SecretBox, connection_id: str, credentials: Credentials, now: datetime
    ) -> None:
        sealed = box.seal(credentials.to_bytes(), aad=_aad(connection_id))
        self._state.execute(
            "INSERT INTO broker_credentials (connection_id, key_id, ciphertext, created_at)"
            " VALUES (?, ?, ?, ?) ON CONFLICT (connection_id) DO UPDATE SET"
            " key_id = excluded.key_id, ciphertext = excluded.ciphertext,"
            " rotated_at = excluded.created_at",
            [connection_id, sealed.key_id, sealed.token, _iso(now)],
        )

    def _open_credentials(self, box: SecretBox, connection_id: str) -> Credentials:
        rows = self._state.sql(
            "SELECT ciphertext FROM broker_credentials WHERE connection_id = ?", [connection_id]
        )
        if not rows:
            raise ConnectionsError("no stored credentials; reconnect")
        raw = box.open(_parse_sealed(rows[0][0]), aad=_aad(connection_id))
        return Credentials.from_bytes(raw)

    def _upsert_accounts(
        self, connection_id: str, accounts: list[ExternalAccount], now: datetime
    ) -> None:
        for a in accounts:
            self._state.execute(
                "INSERT INTO broker_accounts (connection_id, external_account_id, name,"
                " institution, number_mask, currency, first_seen_at, last_seen_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (connection_id, external_account_id)"
                " DO UPDATE SET name = excluded.name, institution = excluded.institution,"
                " number_mask = excluded.number_mask, currency = excluded.currency,"
                " last_seen_at = excluded.last_seen_at",
                [connection_id, a.id, a.name, a.institution, a.number_mask, a.currency,
                 _iso(now), _iso(now)],
            )  # fmt: skip

    def _clear_state(self, connection_id: str, now: datetime) -> None:
        self._state.execute(
            "UPDATE broker_connections SET pending_state_hash = NULL, pending_expires_at = NULL,"
            " updated_at = ? WHERE id = ?",
            [_iso(now), connection_id],
        )


# ---- module helpers --------------------------------------------------------------------


def owned_connection(state: SqliteState, scope: Scope, connection_id: str) -> ConnectionRecord:
    """The connection if ``scope`` may act on it, else :class:`NotFound`
    (missing and not-yours read the same)."""
    if scope.is_service:
        rows = state.sql("SELECT * FROM broker_connections WHERE id = ?", [connection_id])
    else:
        rows = state.sql(
            "SELECT c.* FROM broker_connections c JOIN users u ON u.id = c.user_id"
            " WHERE c.id = ? AND c.user_id = ? AND u.status = 'active'",
            [connection_id, scope.user_id],
        )
    if not rows:
        raise NotFound(f"connection {connection_id!r} not found")
    return ConnectionRecord.from_row(rows[0])


def _fetch(job: _Job, context: ProviderContext) -> _Fetch:
    """Everything a sync reads from the provider. No database access, so it
    can run on a worker thread. Never raises: failures land in ``error``."""
    out = _Fetch(record=job.record)
    if job.error is not None or job.credentials is None:
        out.error = job.error or ConnectionsError("credentials unavailable")
        return out
    credentials = job.credentials
    try:
        conn = job.cls.open(credentials, context)
        try:
            out.accounts = conn.accounts()
            known = {a.id for a in out.accounts}
            for target in job.targets:
                pf = _PortfolioFetch(target)
                if target.account_id not in known:
                    pf.error = f"account {target.account_id} is no longer on this connection"
                else:
                    pf.balances = conn.balances(target.account_id)
                    pf.positions = conn.positions(target.account_id)
                    if conn.supports(Capability.READ_ACTIVITY):
                        pf.activities = conn.activities(target.account_id, target.since)
                out.portfolios.append(pf)
        finally:
            conn.close()
    except ProviderError as exc:
        out.error = _redacted(exc, credentials)
    except ConnectionsError as exc:
        out.error = ConnectionsError(credentials.redact(str(exc)))
    except Exception as exc:  # a provider bug fails this connection, not the pass
        out.error = ConnectionsError(_unexpected(job.record.provider, "sync", exc))
    return out


def _redacted(exc: ProviderError, credentials: Credentials) -> ProviderError:
    """A copy of ``exc`` (same type, status, retry hint) whose message has
    every credential value scrubbed and no chained cause."""
    clean = copy.copy(exc)
    clean.args = (credentials.redact(str(exc)),)
    clean.__cause__ = clean.__context__ = None
    clean.__traceback__ = None
    return clean


def _unexpected(provider: str, what: str, exc: BaseException) -> str:
    """A safe message for an exception we didn't anticipate: its type only,
    since its text may carry vendor payloads or credentials."""
    _log.error("connections.unexpected_error", provider=provider, action=what,
               error_type=type(exc).__name__)  # fmt: skip
    return f"{provider}: could not {what} (unexpected {type(exc).__name__})"


def _require_person(scope: Scope) -> None:
    if scope.is_service:
        raise ConnectionsError("connections belong to people, not services")


def _key_credentials(cls: type[BrokerConnection], fields: Mapping[str, str]) -> Credentials:
    allowed = set(cls.credential_fields) | {"paper"}
    missing = [f for f in cls.credential_fields if not str(fields.get(f) or "").strip()]
    if missing:
        raise ConnectionsError(f"{cls.provider} needs: {', '.join(missing)}")
    extra = sorted(set(fields) - allowed)
    if extra:
        raise ConnectionsError(f"unexpected credential fields: {', '.join(extra)}")
    return Credentials({k: str(v).strip() for k, v in fields.items()})


def _check_redirect(uri: str) -> None:
    parts = urlsplit(uri or "")
    loopback = parts.scheme == "http" and (parts.hostname or "") in _LOOPBACK_HOSTS
    if not parts.netloc or (parts.scheme != "https" and not loopback):
        raise ConnectionsError("the callback URL must be https (http only on loopback)")
    if parts.username or parts.password or parts.fragment:
        raise ConnectionsError("the callback URL must not carry credentials or a fragment")


def _with_query(uri: str, **params: str) -> str:
    parts = urlsplit(uri)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in params]
    query += list(params.items())
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def _hash_state(state: str) -> str:
    return hashlib.sha256(state.encode()).hexdigest()


def _aad(connection_id: str) -> str:
    return f"broker_credentials:{connection_id}"


def _parse_sealed(token: str) -> Any:
    from stonks.security import Sealed

    return Sealed.parse(token)


def _new_id() -> str:
    return f"con_{uuid.uuid4().hex[:12]}"


def _short(text: str) -> str:
    return text if len(text) <= _MAX_ERROR else text[: _MAX_ERROR - 1] + "…"


__all__ = [
    "ConnectionService",
    "backoff",
    "next_sync_after",
    "owned_connection",
    "trading_date",
]
