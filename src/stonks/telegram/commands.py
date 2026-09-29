"""What each bot command does (roadmap 20.3).

Every command of a linked chat acts as the linked user through the app
services, with a :class:`~stonks.auth.Principal` built for that user
(``via = "telegram"``): the user's role and its scopes, never a fresh
second factor. So a command sees only that user's portfolios
(``PortfolioService.resolve``), a viewer cannot engage the kill switch,
and step-up actions (resuming the kill switch) stay in the web app.

Unlinked chats may only ``/start``, ``/link`` and ``/help``. The bot only
answers private chats, so a group cannot act as someone.

``/kill`` asks for a typed confirmation (:data:`KILL_PHRASE`). Only the
next message of that chat, within ``kill_confirm_minutes``, confirms it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from stonks.accounts import NotFound, UserRepository
from stonks.app.context import AppContext
from stonks.app.errors import AppError
from stonks.app.halts import HaltService, KillSwitchRequest
from stonks.app.portfolio import PortfolioService
from stonks.app.strategy_names import strategy_title
from stonks.auth.errors import PermissionDenied
from stonks.auth.policy import Permission, allowed
from stonks.auth.principal import ROLE_SCOPES, Principal
from stonks.logging import get_logger
from stonks.production.ledger import ledger_filter
from stonks.production.pnl import load_pnl
from stonks.store.state import SqliteState
from stonks.telegram.api import TelegramUpdate
from stonks.telegram.links import LinkError, LinkStore
from stonks.telegram.settings import TelegramConfig

_log = get_logger("stonks.telegram.commands")

#: What a person types after ``/kill`` to stop trading on all their books.
KILL_PHRASE = "KILL ALL"
MAX_ROWS = 15

HELP_LINKED = (
    "Stonks commands:\n"
    "/status - halts, last trading run and your portfolio value\n"
    "/today - today's orders, fills and P&L change\n"
    "/positions [portfolio_id] - your holdings\n"
    "/signals - latest signals of your strategies\n"
    "/kill - stop new orders on all your portfolios (asks you to type KILL ALL)\n"
    "/unlink - unlink this chat\n"
    "Resuming after Stop trading needs the web app and a fresh second factor."
)
HELP_UNLINKED = (
    "This chat is not linked to Stonks yet. Make a link code in the web app "
    "(Settings, Telegram) or with `stonks telegram link-code`, then send /link CODE."
)

Clock = Callable[[], datetime]


def principal_for(state: SqliteState, user_id: str) -> Principal | None:
    """The linked user as a principal, or ``None`` when the account is
    gone or disabled."""
    try:
        user = UserRepository(state).get(user_id)
    except NotFound:
        return None
    if user.status != "active":
        return None
    return Principal.create(
        user_id=user.id,
        kind=user.kind,
        role=user.role,
        scopes=ROLE_SCOPES[user.role],
        mfa_fresh=False,
        via="telegram",
    )


def _money(value: float, currency: str) -> str:
    return f"{value:,.2f} {currency}"


class CommandHandler:
    def __init__(
        self,
        context: AppContext,
        config: TelegramConfig | None = None,
        *,
        clock: Clock = lambda: datetime.now(UTC),
    ) -> None:
        self._ctx = context
        self._config = config or TelegramConfig()
        self._clock = clock
        self._portfolios = PortfolioService(context)
        self._halts = HaltService(context)
        #: chat id -> when its pending ``/kill`` confirmation expires.
        self._pending_kill: dict[str, datetime] = {}

    # ---- entry point ------------------------------------------------------------------

    def handle(self, update: TelegramUpdate) -> str | None:
        """The reply to one message, or ``None`` to stay silent."""
        if update.chat_type != "private":
            return "Stonks only answers private chats."
        text = update.text.strip()
        chat = update.chat_id
        pending = self._pending_kill.pop(chat, None)
        with self._ctx.state() as state:
            link = LinkStore(state).for_chat(chat)
            if pending is not None and link is not None and not text.startswith("/"):
                return self._confirm_kill(state, link.user_id, text, pending)
            command, _, arg = text.partition(" ")
            command = command.split("@", 1)[0].lower()
            arg = arg.strip()
            if command in ("/start", "/link"):
                return self._link(state, chat, update.username, arg, linked=link is not None)
            if command == "/help" or link is None:
                return HELP_LINKED if link is not None else HELP_UNLINKED
            if command == "/unlink":
                LinkStore(state).unlink_chat(chat, actor=f"user:{link.user_id}")
                return "This chat is unlinked. Nothing more will be sent here."
            principal = principal_for(state, link.user_id)
        if principal is None:
            return "Your Stonks account is not active."
        handlers = {
            "/status": self._status,
            "/today": self._today,
            "/positions": self._positions,
            "/signals": self._signals,
            "/kill": self._kill,
        }
        fn = handlers.get(command)
        if fn is None:
            return "Unknown command. Send /help."
        try:
            return fn(principal, chat, arg)
        except PermissionDenied:
            return "Your role does not allow that."
        except AppError as exc:
            return str(exc)

    # ---- linking --------------------------------------------------------------------

    def _link(
        self, state: SqliteState, chat: str, username: str | None, code: str, *, linked: bool
    ) -> str:
        if not code:
            return HELP_LINKED if linked else HELP_UNLINKED
        try:
            LinkStore(state).redeem(code, chat, username, now=self._clock())
        except LinkError as exc:
            return str(exc)
        return "Linked. This chat now gets your Stonks notifications. Send /help."

    # ---- commands -------------------------------------------------------------------

    def _book(self, principal: Principal, portfolio_id: str | None = None) -> str:
        return self._portfolios.resolve(principal, portfolio_id or None)

    def _status(self, principal: Principal, chat: str, arg: str) -> str:
        halts = [h for h in self._halts.list(principal) if h.active]
        lines = []
        if halts:
            lines.append(f"Halts in force: {len(halts)}")
            lines += [f"- {h.kind} ({h.scope}, {h.halt}): {h.reason}" for h in halts[:MAX_ROWS]]
        else:
            lines.append("No halt in force.")
        with self._ctx.state() as state:
            ticks = state.sql(
                "SELECT started_at, status FROM tick_runs ORDER BY started_at DESC LIMIT 1"
            )
        if ticks:
            lines.append(f"Last tick: {ticks[0]['status']} at {ticks[0]['started_at'][:16]}")
        else:
            lines.append("No tick has run yet.")
        try:
            pid = self._book(principal)
        except AppError:
            lines.append("You have no portfolio yet.")
            return "\n".join(lines)
        book = self._portfolios.current(pid)
        lines.append(f"Portfolio {pid}: {_money(book.total_value, book.currency)}")
        return "\n".join(lines)

    def _today(self, principal: Principal, chat: str, arg: str) -> str:
        pid = self._book(principal, arg)
        today = self._clock().date()
        with self._ctx.state() as state:
            where, params = ledger_filter(state, "orders", pid)
            orders = state.sql(
                f"SELECT ticker, side, quantity, status FROM orders WHERE {where}"
                " AND substr(created_at, 1, 10) = ? ORDER BY created_at",
                [*params, today.isoformat()],
            )
            fwhere, fparams = ledger_filter(state, "fills", pid)
            fills = state.sql(
                f"SELECT COUNT(*) AS n FROM fills WHERE {fwhere} AND substr(filled_at, 1, 10) = ?",
                [*fparams, today.isoformat()],
            )[0]["n"]
            pnl = load_pnl(state, portfolio_id=pid)
        lines = [f"Today ({today.isoformat()}), portfolio {pid}:"]
        lines.append(f"Orders: {len(orders)}, fills: {fills}")
        lines += [
            f"- {r['side']} {r['quantity']:g} {r['ticker']} ({r['status']})"
            for r in orders[:MAX_ROWS]
        ]
        last = pnl[-1] if pnl else None
        if last is not None and last.day == today and last.daily_change is not None:
            pct = f" ({last.daily_return:+.2%})" if last.daily_return is not None else ""
            lines.append(f"P&L change: {last.daily_change:+,.2f}{pct}")
        else:
            lines.append("No P&L for today yet.")
        return "\n".join(lines)

    def _positions(self, principal: Principal, chat: str, arg: str) -> str:
        pid = self._book(principal, arg)
        book = self._portfolios.current(pid)
        if not book.positions:
            return f"Portfolio {pid} holds no positions. Cash {_money(book.cash, book.currency)}."
        lines = [f"Portfolio {pid}: {_money(book.total_value, book.currency)}"]
        for p in book.positions[:MAX_ROWS]:
            value = f" = {p.market_value:,.2f}" if p.market_value is not None else ""
            lines.append(f"- {p.ticker}: {p.quantity:g}{value}")
        if len(book.positions) > MAX_ROWS:
            lines.append(f"... and {len(book.positions) - MAX_ROWS} more")
        lines.append(f"Cash: {_money(book.cash, book.currency)}")
        return "\n".join(lines)

    def _signals(self, principal: Principal, chat: str, arg: str) -> str:
        with self._ctx.state() as state:
            subs = state.sql(
                "SELECT DISTINCT strategy_id FROM subscriptions WHERE user_id = ? AND enabled = 1",
                [principal.user_id],
            )
            ids = [r["strategy_id"] for r in subs]
            if not ids:
                return "You follow no strategy yet."
            marks = ",".join("?" for _ in ids)
            rows = state.sql(
                f"SELECT as_of, strategy_id, ticker, kind, reason_json FROM signal_events"
                f" WHERE strategy_id IN ({marks}) ORDER BY as_of DESC, id DESC LIMIT ?",
                [*ids, MAX_ROWS],
            )
        if not rows:
            return "No signals yet for the strategies you follow."
        lines = ["Latest signals:"]
        for r in rows:
            name = strategy_title(r["strategy_id"]) or r["strategy_id"]
            lines.append(f"- {r['as_of']} {r['ticker']}: {r['kind']} ({name})")
        return "\n".join(lines)

    def _kill(self, principal: Principal, chat: str, arg: str) -> str:
        if not allowed(principal, Permission.KILLSWITCH_USER):
            return "Your role cannot use Stop trading."
        minutes = self._config.kill_confirm_minutes
        self._pending_kill[chat] = self._clock() + timedelta(minutes=minutes)
        return (
            f"This stops new orders on ALL your portfolios and cancels working orders. "
            f"Type {KILL_PHRASE} within {minutes:g} minutes to confirm. Anything else cancels."
        )

    def _confirm_kill(self, state: SqliteState, user_id: str, text: str, expires: datetime) -> str:
        if self._clock() > expires:
            return "The Stop trading request expired. Send /kill again."
        if text != KILL_PHRASE:
            return "Stop trading cancelled."
        principal = principal_for(state, user_id)
        if principal is None:
            return "Your Stonks account is not active."
        try:
            view = self._halts.engage_kill(
                principal,
                KillSwitchRequest(scope="user", reason="Stop trading from Telegram"),
            )
        except PermissionDenied:
            return "Your role cannot use Stop trading."
        _log.warning("telegram.kill_switch", user_id=user_id, halt_id=view.id)
        return (
            f"Trading stopped (halt #{view.id}). No new orders on your portfolios. "
            "Resume in the web app with a fresh second factor."
        )
