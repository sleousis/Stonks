"""``IbkrBroker``'s options side (roadmap 17.8): the gate, option orders,
combo (``BAG``) orders, combo leg state, option quotes and chains, what-if
margin and option events.

Kept apart from ``broker.py`` so the stock path stays as it was. Every
function takes the broker; ``IbkrBroker`` delegates to them.

**The gate.** An option order that may open a position needs the
portfolio's :class:`~stonks.options.live.gate.OptionsLiveState` to allow
it (the switch, a live stage and an approval level). A broker built
without a gate refuses every opening option order. An order marked
``position_effect = "close"`` must shrink a position the account holds,
checked against IBKR's positions, or it counts as opening.

**Combos.** A combo's legs are ledger orders ``<combo id>:<i>``. The
``BAG`` goes out under the combo id as its ``orderRef``. IBKR reports one
execution per leg with that reference: leg ``i`` is the ``BAG``'s leg
``i``, matched by ``conId``. So a leg's state and fills are read from the
``BAG`` order, and reconciliation books them on the leg's own client id.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from datetime import date
from typing import TYPE_CHECKING

from stonks.core.clock import today
from stonks.core.combos import ComboOrder
from stonks.core.options import STANDARD_MULTIPLIER, is_option_id, parse_contract_id
from stonks.core.types import Order
from stonks.execution.brokers.base import (
    BrokerOrderState,
    LiveTradingRefusedError,
    MarginPreview,
    OrderOutcomeUnknownError,
    OrderRejectedError,
    UnsupportedTickerError,
)
from stonks.execution.brokers.ibkr.client import (
    IbApiError,
    IbContract,
    IbContractQuery,
    IbExecution,
    IbOptionDataClient,
    IbOptionEvent,
    IbOptionEventClient,
    IbOptionSnapshot,
    IbTrade,
)
from stonks.execution.brokers.ibkr.contracts import ResolvedContract
from stonks.execution.brokers.ibkr.errors import classify, to_broker_error
from stonks.execution.brokers.ibkr.options import (
    combo_contract,
    matches,
    option_id_for_contract,
    to_ib_combo_order,
    to_ib_option_order,
)
from stonks.execution.order_state import ledger_status
from stonks.ingest.option_schemas import OptionQuoteRow
from stonks.logging import get_logger
from stonks.options.chain import OptionQuote
from stonks.options.live.events import OptionEvent

if TYPE_CHECKING:
    from stonks.execution.brokers.ibkr.broker import IbkrBroker

_log = get_logger("stonks.execution.brokers.ibkr.options")

_LEG_ID = re.compile(r"^(?P<combo>.+):(?P<index>\d+)$")
#: Contracts per snapshot request, well under IBKR's 100 market data lines.
SNAPSHOT_BATCH = 50


def is_option_order(order: Order) -> bool:
    return is_option_id(order.ticker)


# ---- the gate ------------------------------------------------------------------------


def _held(broker: IbkrBroker) -> dict[str, float]:
    account = broker.account_id
    held: dict[str, float] = {}
    for p in broker.guarded("positions", lambda: broker.client.positions(account)):
        if p.account and p.account != account:
            continue
        ticker = broker.ticker_of(p.contract)
        held[ticker] = held.get(ticker, 0.0) + p.position
    return held


def _closes(held: Mapping[str, float], ticker: str, signed_qty: float) -> bool:
    have = held.get(ticker, 0.0)
    return have * signed_qty < 0 and abs(signed_qty) <= abs(have) + 1e-9


def ensure_options_allowed(broker: IbkrBroker, legs: Mapping[str, float], closing: bool) -> None:
    """Refuse unless the legs only close held positions, or the gate lets
    the portfolio open options."""
    if closing:
        held = _held(broker)
        if all(_closes(held, t, q) for t, q in legs.items()):
            return
        _log.warning("ibkr.option.close_not_held", legs=sorted(legs))
    gate = broker.options_gate
    state = gate() if gate is not None else None
    if state is None or not state.allowed:
        why = state.refusal if state is not None else "options live is off for this broker"
        raise LiveTradingRefusedError(why or "options may not open here")


# ---- single-leg option orders -------------------------------------------------------------


def place_option(broker: IbkrBroker, order: Order, account: str) -> None:
    """One option leg as a day limit order (never a market order)."""
    signed = order.quantity if order.side == "buy" else -order.quantity
    ensure_options_allowed(broker, {order.ticker: signed}, order.position_effect == "close")
    _place_checked(broker, order, account)


def _place_checked(broker: IbkrBroker, order: Order, account: str) -> None:
    resolved = broker.resolver.resolve(order.ticker)
    request = to_ib_option_order(
        order, tick=resolved.min_tick, account=account, settings=broker.order_settings
    )
    _submit(broker, order.client_id, resolved.contract, request)


def _submit(broker: IbkrBroker, client_id: str, contract: IbContract, request: object) -> None:
    from stonks.execution.brokers.ibkr.client import IbOrderRequest

    assert isinstance(request, IbOrderRequest)
    try:
        broker.client.place_order(contract, request)
    except IbApiError as exc:
        if classify(exc.code) == "duplicate_order_id" and (
            broker.get_order_state(client_id) is not None
        ):
            return
        raise to_broker_error(exc, action=f"submit of {client_id}") from exc
    except (ConnectionError, TimeoutError) as exc:
        raise OrderOutcomeUnknownError(
            client_id,
            f"submit of {client_id} did not finish ({type(exc).__name__}); "
            "reconcile before sending anything for it",
        ) from exc
    _log.info("ibkr.option.submitted", client_id=client_id, sec_type=contract.sec_type)


# ---- combos ------------------------------------------------------------------------------


def _leg_contracts(broker: IbkrBroker, combo: ComboOrder) -> list[ResolvedContract]:
    return [broker.resolver.resolve(leg.instrument) for leg in combo.legs]


def place_combo(broker: IbkrBroker, combo: ComboOrder) -> None:
    """A combo as one ``BAG`` order at its net limit. A one-leg combo goes
    out as a plain option order under its leg's client id."""
    account = broker.ensure_may_trade()
    ensure_options_allowed(broker, combo.signed_quantities(), combo.effect == "close")
    if len(combo.legs) == 1:
        leg_order = combo.leg_orders()[0]
        if combo.net_limit is None:
            raise OrderRejectedError(f"{combo.client_id}: a combo needs a net limit")
        price = abs(combo.net_limit) / combo.legs[0].ratio
        order = Order(
            client_id=leg_order.client_id,
            ticker=leg_order.ticker,
            side=leg_order.side,
            quantity=leg_order.quantity,
            order_type="limit",
            limit_price=price,
            position_effect=leg_order.position_effect,
            strategy_id=leg_order.strategy_id,
            decision_context=leg_order.decision_context,
        )
        if broker.get_order_state(order.client_id) is not None:
            return
        _place_checked(broker, order, account)
        return
    ref = broker.broker_ref(combo.client_id)
    broker.remember_ref(ref, combo.client_id)
    if _bag_trade(broker, ref) is not None or any(
        e.order_ref == ref for e in broker.guarded("executions", broker.client.executions)
    ):
        _log.info("ibkr.combo.already_at_broker", client_id=combo.client_id)
        return
    resolved = _leg_contracts(broker, combo)
    currency = next((r.contract.currency for r in resolved if r.contract.sec_type == "OPT"), "USD")
    contract = combo_contract(combo, [r.con_id for r in resolved], currency)
    tick = min((r.min_tick for r in resolved if r.contract.sec_type == "OPT"), default=0.01)
    request = to_ib_combo_order(combo, tick=tick, account=account, settings=broker.order_settings)
    broker.bag_legs[ref] = tuple(r.con_id for r in resolved)
    _submit(broker, combo.client_id, contract, request)


def _bag_trade(broker: IbkrBroker, ref: str) -> IbTrade | None:
    trade = broker.find_trade(ref)
    if trade is None or trade.contract.sec_type != "BAG":
        return None
    broker.bag_legs[ref] = tuple(leg.con_id for leg in trade.contract.combo_legs)
    return trade


def bag_ref(broker: IbkrBroker, client_id: str) -> str | None:
    """The ``orderRef`` of the ``BAG`` order that holds combo leg
    ``<combo>:<i>``, or ``None`` for anything that is not a leg id."""
    m = _LEG_ID.match(client_id)
    return broker.broker_ref(m.group("combo")) if m is not None else None


def leg_state(broker: IbkrBroker, client_id: str) -> BrokerOrderState | None:
    """The state of combo leg ``<combo>:<i>`` from its ``BAG`` order."""
    m = _LEG_ID.match(client_id)
    if m is None:
        return None
    ref = broker.broker_ref(m.group("combo"))
    trade = _bag_trade(broker, ref)
    index = int(m.group("index"))
    if trade is None or index >= len(trade.contract.combo_legs):
        return None
    leg = trade.contract.combo_legs[index]
    execs = [
        e
        for e in broker.guarded("executions", broker.client.executions)
        if e.order_ref == ref and e.contract.con_id == leg.con_id
    ]
    filled = sum(e.shares for e in execs)
    avg = sum(e.shares * e.price for e in execs) / filled if filled > 0 else None
    state = broker.state_of(trade, trade.filled)
    ticker = broker.resolver.ticker_for(leg.con_id)
    if ticker is None:
        ticker = broker.ticker_of(execs[0].contract) if execs else str(leg.con_id)
    return BrokerOrderState(
        client_id=client_id,
        broker_order_id=str(trade.perm_id),
        ticker=ticker,
        side="buy" if leg.action == "BUY" else "sell",
        status=ledger_status(state),
        quantity=trade.total_quantity * leg.ratio,
        filled_quantity=filled,
        avg_fill_price=avg,
        state=state,
    )


def leg_client_id(broker: IbkrBroker, e: IbExecution, client_id: str) -> str:
    """The leg's client id for an execution of a ``BAG`` order (its own
    client id for anything else)."""
    ref = e.order_ref
    legs = broker.bag_legs.get(ref)
    if legs is None:
        if e.contract.sec_type not in ("OPT", "STK") or _LEG_ID.match(client_id):
            return client_id
        if ref in broker.not_bags:
            return client_id
        trade = _bag_trade(broker, ref)
        if trade is None:
            broker.not_bags.add(ref)
            return client_id
        legs = broker.bag_legs[ref]
    try:
        return f"{client_id}:{legs.index(e.contract.con_id)}"
    except ValueError:
        return client_id


def what_if_combo(broker: IbkrBroker, combo: ComboOrder) -> MarginPreview:
    """IBKR's preview of a combo (one ``BAG`` what-if). Raises on failure:
    the caller must never let an opening combo through on it."""
    account = broker.account_id
    resolved = _leg_contracts(broker, combo)
    if len(resolved) == 1:
        leg = combo.leg_orders()[0]
        if combo.net_limit is None:
            raise OrderRejectedError(f"{combo.client_id}: a combo needs a net limit")
        order = Order(
            client_id=leg.client_id,
            ticker=leg.ticker,
            side=leg.side,
            quantity=leg.quantity,
            order_type="limit",
            limit_price=abs(combo.net_limit) / combo.legs[0].ratio,
        )
        contract = resolved[0].contract
        request = to_ib_option_order(
            order, tick=resolved[0].min_tick, account=account, settings=broker.order_settings
        )
    else:
        currency = next(
            (r.contract.currency for r in resolved if r.contract.sec_type == "OPT"), "USD"
        )
        contract = combo_contract(combo, [r.con_id for r in resolved], currency)
        tick = min((r.min_tick for r in resolved if r.contract.sec_type == "OPT"), default=0.01)
        request = to_ib_combo_order(
            combo, tick=tick, account=account, settings=broker.order_settings
        )
    answer = broker.guarded(
        f"what-if of {combo.client_id}", lambda: broker.client.what_if(contract, request)
    )
    return MarginPreview(
        client_id=combo.client_id,
        initial_margin_change=_greek(answer.init_margin_change) or 0.0,
        maintenance_margin_change=_greek(answer.maint_margin_change) or 0.0,
        equity_with_loan_after=_greek(answer.equity_with_loan_after) or 0.0,
        commission=_greek(answer.commission),
        commission_currency=answer.commission_currency or None,
        warning=answer.warning or None,
    )


# ---- quotes and chains -----------------------------------------------------------------


def _data_client(broker: IbkrBroker) -> IbOptionDataClient | None:
    client = broker.client
    return client if isinstance(client, IbOptionDataClient) else None


def _snapshots(
    broker: IbkrBroker, client: IbOptionDataClient, contracts: Sequence[IbContract]
) -> list[IbOptionSnapshot]:
    out: list[IbOptionSnapshot] = []
    for start in range(0, len(contracts), SNAPSHOT_BATCH):
        batch = list(contracts[start : start + SNAPSHOT_BATCH])
        try:
            out.extend(client.option_snapshots(batch))
        except IbApiError as exc:
            if classify(exc.code) == "no_market_data":
                _log.warning("ibkr.option_quotes.no_subscription", code=exc.code)
                return out
            raise to_broker_error(exc, action="option quotes") from exc
        except (ConnectionError, TimeoutError) as exc:
            raise to_broker_error(exc, action="option quotes") from exc
    return out


def _price(value: float | None) -> float | None:
    if value is None or not math.isfinite(value) or value < 0 or value >= 1e300:
        return None
    return float(value)


def _greek(value: float | None) -> float | None:
    if value is None or not math.isfinite(value) or abs(value) >= 1e300:
        return None
    return float(value)


def _quote(contract_id: str, s: IbOptionSnapshot, as_of: date) -> OptionQuote:
    iv = _greek(s.iv)
    return OptionQuote(
        contract=parse_contract_id(contract_id),
        as_of=as_of,
        bid=_price(s.bid),
        ask=_price(s.ask),
        last=_price(s.last),
        volume=_price(s.volume),
        open_interest=_price(s.open_interest),
        underlying_price=_price(s.underlying_price) or None,
        iv=iv if iv is not None and iv > 0 else None,
        delta=_greek(s.delta),
        gamma=_greek(s.gamma),
        theta=_greek(s.theta),
        vega=_greek(s.vega),
    )


def option_quotes(
    broker: IbkrBroker, contract_ids: Sequence[str], as_of: date | None = None
) -> dict[str, OptionQuote]:
    """Live quotes of ``contract_ids`` with IBKR's model Greeks. Needs the
    OPRA market data add-on for live values; a contract with no answer is
    left out."""
    broker.ensure_ready()
    client = _data_client(broker)
    if client is None:
        return {}
    day = as_of or today(broker.clock)
    by_con: dict[int, str] = {}
    contracts: list[IbContract] = []
    for cid in contract_ids:
        try:
            resolved = broker.resolver.resolve(cid)
        except UnsupportedTickerError as exc:
            _log.warning("ibkr.option_quote.unsupported", contract_id=cid, error=str(exc))
            continue
        by_con[resolved.con_id] = cid
        contracts.append(resolved.contract)
    return {
        by_con[s.con_id]: _quote(by_con[s.con_id], s, day)
        for s in _snapshots(broker, client, contracts)
        if s.con_id in by_con
    }


def option_chain(
    broker: IbkrBroker,
    underlying: str,
    as_of: date | None = None,
    *,
    max_expiry_days: int = 120,
    strike_band: float = 0.3,
) -> list[OptionQuoteRow]:
    """Today's chain of ``underlying`` from IBKR: the expiries within
    ``max_expiry_days`` and the strikes within ``strike_band`` of spot,
    with quotes and IBKR's Greeks. One contract lookup per expiry."""
    broker.ensure_ready()
    client = _data_client(broker)
    if client is None:
        return []
    day = as_of or today(broker.clock)
    stock = broker.resolver.resolve(underlying)
    spot_quote = broker.quotes([underlying]).get(underlying)
    spot = spot_quote.reference if spot_quote is not None else None
    try:
        params = client.option_params(stock.contract.symbol, stock.con_id)
    except IbApiError as exc:
        raise to_broker_error(exc, action=f"option chain of {underlying}") from exc
    expiries: set[str] = set()
    for p in params:
        if p.multiplier not in ("100", "100.0"):
            continue
        for text in p.expirations:
            try:
                expiry = date(int(text[:4]), int(text[4:6]), int(text[6:8]))
            except ValueError:
                continue
            if 0 <= (expiry - day).days <= max_expiry_days:
                expiries.add(text)
    contracts: dict[int, str] = {}
    resolved: list[IbContract] = []
    for text in sorted(expiries):
        query = IbContractQuery(
            symbol=stock.contract.symbol,
            currency=stock.contract.currency,
            sec_type="OPT",
            last_trade_date=text,
            multiplier="100",
        )
        for d in broker.resolver.details(query):
            cid = option_id_for_contract(d.contract)
            if cid is None:
                continue
            contract = parse_contract_id(cid)
            if contract.underlying != underlying or not matches(d, contract):
                continue
            if contract.multiplier != STANDARD_MULTIPLIER:
                continue  # an adjusted contract is never picked from a chain
            if spot and abs(contract.strike / spot - 1.0) > strike_band:
                continue
            broker.resolver.cache.put(
                ResolvedContract(
                    ticker=cid,
                    contract=d.contract,
                    min_tick=d.min_tick,
                    verified_at=broker.clock.now(),
                )
            )
            contracts[d.contract.con_id] = cid
            resolved.append(d.contract)
    rows: list[OptionQuoteRow] = []
    for s in _snapshots(broker, client, resolved):
        cid = contracts.get(s.con_id)
        if cid is None:
            continue
        q = _quote(cid, s, day)
        c = q.contract
        rows.append(
            OptionQuoteRow(
                underlying=c.underlying,
                expiry=c.expiry,
                strike=c.strike,
                right=c.right,
                multiplier=c.multiplier,
                currency=c.currency,
                exchange="SMART",
                as_of=day,
                bid=q.bid,
                ask=q.ask,
                last=q.last,
                volume=q.volume,
                open_interest=q.open_interest,
                underlying_price=q.underlying_price or spot or None,
                iv=q.iv,
                delta=q.delta,
                gamma=q.gamma,
                theta=q.theta,
                vega=q.vega,
            )
        )
    return rows


# ---- option events --------------------------------------------------------------------


def option_events(broker: IbkrBroker) -> list[OptionEvent]:
    """Assignments, exercises and expiries of this account's options, from
    the event source (the Flex statement's ``OptionEAE`` rows, or the fake
    gateway). An event on a contract we cannot name is logged and left out."""
    account = broker.account_id
    raw: list[IbOptionEvent] = []
    client = broker.client
    if isinstance(client, IbOptionEventClient):
        raw.extend(broker.guarded("option events", client.option_events))
    if broker.option_event_reader is not None:
        raw.extend(broker.option_event_reader())
    out: dict[str, OptionEvent] = {}
    for e in raw:
        if e.account and e.account != account:
            continue
        cid = broker.resolver.ticker_for(e.contract.con_id) or option_id_for_contract(e.contract)
        if cid is None or not is_option_id(cid):
            _log.warning("ibkr.option_event.unmapped", con_id=e.contract.con_id)
            continue
        out[e.event_id] = OptionEvent(
            event_id=e.event_id,
            kind=e.kind,
            contract_id=cid,
            quantity=float(e.quantity),
            occurred_on=e.time.date(),
            source="ibkr",
        )
    return list(out.values())
