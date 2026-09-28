"""The ``options_live`` and ``options_expiry_watch`` jobs (roadmap 17.8).

Both skip while ``[production.options] live = false`` (the default).

``options_live`` runs after the close, per live option portfolio (a broker
portfolio at ``live_small`` or higher, or one with an approval level):

1. open its broker; one without the options capability is skipped;
2. book the broker's assignments, exercises and expiries into the ledger
   (``events.py``) and tell the owner;
3. plan the expiry closes and rolls (``expiry.py``);
4. price every combo at the mid from live quotes (``pricing.py``); a combo
   with no usable quote is not made, and a short that cannot be closed is
   reported;
5. run the option risk rules on the live book (``risk.py``); an opening
   combo also needs the gate (``gate.py``);
6. preview each combo's margin with the broker's what-if;
7. write one ticket per leg. Option tickets wait for a person (hold
   ``options``) unless ``auto_approve_closes`` lets a closing combo go
   out approved by the system. The ``live_submit`` job sends approved
   tickets before the next open, a multi-leg combo as one order.

``options_expiry_watch`` runs on expiry days before the close: a short
option that expires today, still held and in or near the money, raises a
high urgency alert. It never sends an order.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Literal

from stonks.core.clock import SYSTEM_CLOCK, Clock
from stonks.core.combos import ComboOrder
from stonks.core.options import OptionContract, is_option_id, parse_contract_id
from stonks.core.protocols import Broker
from stonks.execution.brokers.base import QuoteSource
from stonks.logging import get_logger
from stonks.options.chain import OptionQuote
from stonks.options.live.approval import approvals_enabled, get_approval
from stonks.options.live.broker import OptionBroker
from stonks.options.live.events import BookedEvent, book_events
from stonks.options.live.expiry import (
    ExpiryWarning,
    SessionsLeft,
    calendar_sessions,
    expiry_watch,
    plan_expiry,
)
from stonks.options.live.gate import options_live_state
from stonks.options.live.orders import to_leg_orders
from stonks.options.live.pricing import price_combo
from stonks.options.live.risk import (
    check_combos,
    contract_prices,
    live_policy,
    live_view,
)
from stonks.options.live.settings import OptionsLiveSettings
from stonks.production.live.settings import LiveSettings
from stonks.production.live.stages import get_stage, stages_enabled
from stonks.store.state import SqliteState

_log = get_logger("stonks.options.live.run")

Phase = Literal["plan", "watch"]
Publish = Callable[[Any], object]
BrokerOpener = Callable[[str], Broker]

#: Why a ticket of a live option order waits for a person.
OPTIONS_HOLD = "options"


@dataclass(frozen=True)
class PortfolioOptionsRun:
    portfolio_id: str
    status: Literal["ok", "skipped", "error"]
    reason: str | None = None
    events_booked: int = 0
    tickets: int = 0
    awaiting_approval: int = 0
    #: Combo client id -> why no ticket was written for it.
    dropped: Mapping[str, str] = field(default_factory=dict[str, str])
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class OptionsLiveRun:
    enabled: bool
    phase: Phase = "plan"
    portfolios: tuple[PortfolioOptionsRun, ...] = ()

    def detail(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "phase": self.phase,
            "portfolios": {
                p.portfolio_id: {
                    "status": p.status,
                    "reason": p.reason,
                    "events_booked": p.events_booked,
                    "tickets": p.tickets,
                    "awaiting_approval": p.awaiting_approval,
                    "dropped": dict(p.dropped),
                    "warnings": list(p.warnings),
                }
                for p in self.portfolios
            },
        }

    @property
    def errors(self) -> dict[str, str]:
        return {p.portfolio_id: p.reason or "" for p in self.portfolios if p.status == "error"}


def option_portfolios(state: SqliteState) -> list[str]:
    """Broker portfolios at a real-money stage, and any with an approval
    level above ``none`` (so a demoted book still gets its expiry care)."""
    ids: set[str] = set()
    if stages_enabled(state):
        rows = state.sql(
            "SELECT id FROM portfolios WHERE kind = 'broker' AND status = 'active'"
            " AND live_stage IN ('live_small', 'live_scale')"
        )
        ids |= {r["id"] for r in rows}
    if approvals_enabled(state):
        rows = state.sql(
            "SELECT a.portfolio_id FROM option_approvals a JOIN portfolios p"
            " ON p.id = a.portfolio_id WHERE a.level <> 'none' AND p.status = 'active'"
        )
        ids |= {r["portfolio_id"] for r in rows}
    return sorted(ids)


def run_options_live(
    state: SqliteState,
    open_broker: BrokerOpener,
    *,
    settings: OptionsLiveSettings,
    live: LiveSettings | None = None,
    risk: Any = None,
    as_of: date,
    phase: Phase = "plan",
    clock: Clock = SYSTEM_CLOCK,
    publish: Publish | None = None,
    sessions_left: SessionsLeft | None = None,
    portfolio_ids: Sequence[str] | None = None,
) -> OptionsLiveRun:
    """Run one phase for every live option portfolio. Never raises for one
    portfolio's broker."""
    if not settings.live:
        return OptionsLiveRun(enabled=False, phase=phase)
    run = _Run(
        state=state,
        settings=settings,
        live=live or LiveSettings(),
        risk=risk,
        as_of=as_of,
        clock=clock,
        publish=publish,
        sessions_left=sessions_left or calendar_sessions((live or LiveSettings()).submit.calendar),
    )
    results: list[PortfolioOptionsRun] = []
    for pid in portfolio_ids if portfolio_ids is not None else option_portfolios(state):
        try:
            broker = open_broker(pid)
        except Exception as exc:
            _log.warning("options.live.broker_unavailable", portfolio_id=pid, error=str(exc))
            results.append(PortfolioOptionsRun(pid, "error", reason=f"{type(exc).__name__}: {exc}"))
            continue
        try:
            if not isinstance(broker, OptionBroker):
                results.append(
                    PortfolioOptionsRun(pid, "skipped", reason="the broker cannot trade options")
                )
                continue
            if phase == "watch":
                results.append(_watch(run, pid, broker))
            else:
                results.append(_plan(run, pid, broker))
        except Exception as exc:
            _log.warning("options.live.failed", portfolio_id=pid, error=str(exc))
            results.append(PortfolioOptionsRun(pid, "error", reason=f"{type(exc).__name__}: {exc}"))
        finally:
            close = getattr(broker, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as exc:  # the run's outcome matters more
                    _log.warning("options.live.close_failed", error=str(exc))
    return OptionsLiveRun(enabled=True, phase=phase, portfolios=tuple(results))


@dataclass(frozen=True)
class _Run:
    state: SqliteState
    settings: OptionsLiveSettings
    live: LiveSettings
    risk: Any
    as_of: date
    clock: Clock
    publish: Publish | None
    sessions_left: SessionsLeft


# ---- plan -----------------------------------------------------------------------------


def _spots(
    broker: Broker, underlyings: set[str], quotes: Mapping[str, OptionQuote]
) -> dict[str, float]:
    spots: dict[str, float] = {}
    if underlyings and isinstance(broker, QuoteSource):
        try:
            for ticker, q in broker.quotes(sorted(underlyings)).items():
                ref = q.reference
                if ref is not None:
                    spots[ticker] = ref
        except Exception as exc:
            _log.warning("options.live.spot_quotes_failed", error=str(exc))
    for q in quotes.values():
        u = q.contract.underlying
        if u not in spots and q.underlying_price:
            spots[u] = q.underlying_price
    return spots


def _plan(run: _Run, pid: str, broker: Any) -> PortfolioOptionsRun:
    state, s = run.state, run.settings
    booked = book_events(state, pid, broker.option_events(), clock=run.clock)
    _tell_events(run, pid, booked)
    account = broker.fetch_portfolio()
    positions = dict(account.positions)

    def chain(underlying: str) -> list[OptionContract]:
        rows = broker.option_chain(
            underlying,
            run.as_of,
            max_expiry_days=s.chain_max_expiry_days,
            strike_band=s.chain_strike_band,
        )
        return [r.contract for r in rows]

    items = plan_expiry(
        positions,
        run.as_of,
        s.expiry,
        portfolio_id=pid,
        sessions_left=run.sessions_left,
        chain=chain if s.expiry.action == "roll" else None,
    )
    combos = [i.combo for i in items]
    if not combos:
        return PortfolioOptionsRun(pid, "ok", events_booked=len(booked))

    wanted = sorted(
        {leg.instrument for c in combos for leg in c.legs if leg.is_option}
        | {k for k in positions if is_option_id(k)}
    )
    quotes = dict(broker.option_quotes(wanted, run.as_of))
    underlyings = {parse_contract_id(k).underlying for k in wanted}
    spots = _spots(broker, underlyings, quotes)

    dropped: dict[str, str] = {}
    warnings: list[str] = []
    priced: list[ComboOrder] = []
    leg_limits: dict[str, dict[str, float]] = {}
    for combo in combos:
        got = price_combo(
            combo, quotes, collar_share=s.collar_share, max_spread_pct=s.max_spread_pct
        )
        if got.combo is None:
            dropped[combo.client_id] = got.reason or "not priced"
            if combo.effect == "close" or combo.structure == "expiry_roll":
                warnings.append(f"cannot price the expiry order for {combo.legs[0].instrument}")
            continue
        priced.append(got.combo)
        leg_limits[combo.client_id] = dict(got.leg_limits or {})

    gate = options_live_state(
        s.live,
        get_stage(state, pid) if stages_enabled(state) else None,
        get_approval(state, pid).level,
    )
    allowed: list[ComboOrder] = []
    for combo in priced:
        if combo.effect != "close" and not gate.allowed:
            dropped[combo.client_id] = gate.refusal or "options may not open"
            if combo.structure == "expiry_roll":
                warnings.append(f"cannot roll {combo.legs[0].instrument}: {gate.refusal}")
            continue
        allowed.append(combo)

    policy = live_policy(_book_policy(state, pid, run.risk), gate.level, s)
    outcome = check_combos(
        allowed,
        portfolio=account,
        prices=contract_prices(quotes, spots),
        view=live_view(run.as_of, quotes, spots),
        policy=policy,
        as_of=run.as_of,
        portfolio_id=pid,
    )
    dropped.update(outcome.dropped)
    tickets, awaiting = _write_tickets(run, pid, broker, list(outcome.kept), positions, leg_limits)
    if warnings:
        _tell(
            run, pid, "Option expiry needs you", "An option expiry order could not be made.", "exp"
        )
    if awaiting:
        _tell(
            run,
            pid,
            "Option orders wait for you",
            f"{awaiting} option order(s) wait for approval.",
            "tkt",
        )
    return PortfolioOptionsRun(
        pid,
        "ok",
        events_booked=len(booked),
        tickets=tickets,
        awaiting_approval=awaiting,
        dropped=dropped,
        warnings=tuple(warnings),
    )


def _write_tickets(
    run: _Run,
    pid: str,
    broker: Any,
    combos: list[ComboOrder],
    positions: Mapping[str, float],
    leg_limits: Mapping[str, Mapping[str, float]],
) -> tuple[int, int]:
    from stonks.production.tickets import submit_window, tickets_recorded, write_tickets

    if not combos or not tickets_recorded(run.state):
        return 0, 0
    orders = []
    previews: dict[str, dict[str, Any]] = {}
    closing: set[str] = set()
    for combo in combos:
        legs = to_leg_orders(combo, positions, leg_limits[combo.client_id])
        what_if = _what_if(broker, combo)
        for o in legs:
            multiplier = float((o.decision_context or {}).get("multiplier", 100.0))
            previews[o.client_id] = {
                "notional": round(o.quantity * float(o.limit_price or 0.0) * multiplier, 2),
                "multiplier": multiplier,
                "net_limit": combo.net_limit,
                "structure": combo.structure,
                "combo_id": combo.client_id,
                **what_if,
            }
            if combo.effect == "close":
                closing.add(o.client_id)
        orders.extend(legs)
    auto = run.settings.auto_approve_closes
    written = write_tickets(
        run.state,
        orders,
        portfolio_id=pid,
        tick_id=None,
        as_of=run.as_of,
        window=submit_window(run.as_of, run.live.submit),
        hold=lambda o: None if auto and o.client_id in closing else OPTIONS_HOLD,  # type: ignore[arg-type,return-value]
        now=datetime.now(UTC),
        previews=previews,
    )
    awaiting = sum(t.awaiting_approval for t in written)
    _log.info("options.live.tickets", portfolio_id=pid, written=len(written), awaiting=awaiting)
    return len(written), awaiting


def _what_if(broker: Any, combo: ComboOrder) -> dict[str, Any]:
    try:
        p = broker.what_if_combo(combo)
    except Exception as exc:
        _log.warning("options.live.what_if_failed", combo_id=combo.client_id, error=str(exc))
        return {"what_if_error": str(exc)[:200]}
    return {
        "what_if": {
            "commission": p.commission,
            "commission_currency": p.commission_currency,
            "initial_margin_change": p.initial_margin_change,
            "maintenance_margin_change": p.maintenance_margin_change,
            "equity_with_loan_after": p.equity_with_loan_after,
            "warning": p.warning,
        }
    }


def _book_policy(state: SqliteState, pid: str, risk: Any) -> Any:
    """The global policy tightened by the owner's and the portfolio's own."""
    from stonks.accounts.book import tighter_of
    from stonks.config import RiskPolicy

    base = risk if risk is not None else RiskPolicy()
    rows = state.sql(
        "SELECT p.risk_policy_json, u.risk_policy_json AS owner_risk_json FROM portfolios p"
        " LEFT JOIN users u ON u.id = p.owner_id WHERE p.id = ?",
        [pid],
    )
    overrides = []
    if rows:
        for key in ("owner_risk_json", "risk_policy_json"):
            raw = rows[0][key]
            if raw:
                overrides.append(json.loads(raw))
    return tighter_of(base, *overrides)


# ---- watch ----------------------------------------------------------------------------


def _watch(run: _Run, pid: str, broker: Any) -> PortfolioOptionsRun:
    account = broker.fetch_portfolio()
    positions = dict(account.positions)
    today_shorts = [
        k
        for k, q in positions.items()
        if q < 0 and is_option_id(k) and parse_contract_id(k).expiry == run.as_of
    ]
    if not today_shorts:
        return PortfolioOptionsRun(pid, "ok")
    quotes = dict(broker.option_quotes(today_shorts, run.as_of))
    underlyings = {parse_contract_id(k).underlying for k in today_shorts}
    spots = _spots(broker, underlyings, quotes)
    found = expiry_watch(positions, run.as_of, run.settings.expiry, spots=spots, quotes=quotes)
    for w in found:
        _log.warning("options.live.expiry_warning", portfolio_id=pid, detail=w.text)
    if found:
        _tell(
            run,
            pid,
            "Short option expires today",
            f"{len(found)} short option(s) expire today in or near the money. Close them or "
            "they may be assigned.",
            "watch",
            level="error",
        )
    return PortfolioOptionsRun(pid, "ok", warnings=tuple(w.text for w in found))


# ---- notices --------------------------------------------------------------------------


def _tell_events(run: _Run, pid: str, booked: Sequence[BookedEvent]) -> None:
    moved = [b for b in booked if b.event.kind in ("assignment", "exercise")]
    if moved:
        _tell(
            run,
            pid,
            "Option assigned or exercised",
            f"{len(moved)} option position(s) were assigned or exercised. Shares moved.",
            "eae",
        )


def _tell(run: _Run, pid: str, title: str, body: str, key: str, *, level: str = "warning") -> None:
    """A high urgency notice for the owner. No tickers or amounts leave
    the server: the details are in the console."""
    from stonks.notify.events import Audience, Event
    from stonks.notify.router import configured_router

    event = Event(
        category="risk",
        level=level,  # type: ignore[arg-type]
        urgency="high",
        title=title,
        body=body,
        audience=Audience.owner_of(pid),
        dedupe_key=f"options:{key}:{pid}:{run.as_of.isoformat()}",
        deep_link="/tickets",
        portfolio_id=pid,
    )
    try:
        (run.publish or configured_router(run.state).publish)(event)
    except Exception as exc:
        _log.error("options.live.notify_failed", portfolio_id=pid, error=str(exc))


__all__ = [
    "OPTIONS_HOLD",
    "ExpiryWarning",
    "OptionsLiveRun",
    "PortfolioOptionsRun",
    "option_portfolios",
    "run_options_live",
]
