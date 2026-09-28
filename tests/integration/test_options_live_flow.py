"""Live options end to end on a fake IB Gateway (roadmap 17.8): the job
books assignments, plans expiry closes and rolls at the mid, runs the
option rules, writes held tickets, and the submit window sends them, a
roll as one BAG order. Hermetic."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from stonks.core.clock import FakeClock
from stonks.execution.brokers.ibkr.broker import IbkrBroker
from stonks.execution.brokers.ibkr.client import IbOptionSnapshot, IbSnapshot
from stonks.options.live.approval import set_approval
from stonks.options.live.expiry import weekday_sessions
from stonks.options.live.gate import gate_lookup
from stonks.options.live.run import option_portfolios, run_options_live
from stonks.options.live.settings import ExpirySettings, OptionsLiveSettings
from stonks.production.submit import submit_tickets
from stonks.production.tickets import decide_tickets, list_tickets
from tests.fakes.ib_gateway import AAPL, FakeIbGateway, option
from tests.integration.test_options_live_gate import promote_to

AS_OF = date(2026, 10, 15)  # Thursday; the October options expire Friday the 16th
EXP = date(2026, 10, 16)
NOV = date(2026, 11, 20)
OPEN = datetime(2026, 10, 16, 13, 30, tzinfo=UTC)
IN_WINDOW = OPEN - timedelta(minutes=15)
ACCOUNT = "U7654321"

P190 = option(1003, "AAPL", EXP, "P", 190.0)
C210 = option(1002, "AAPL", EXP, "C", 210.0)
P190_NOV = option(1013, "AAPL", NOV, "P", 190.0)
P185_NOV = option(1014, "AAPL", NOV, "P", 185.0)
PUT_190 = "AAPL.US:2026-10-16:P:190"
PUT_190_NOV = "AAPL.US:2026-11-20:P:190"
ON = OptionsLiveSettings(live=True)
PID = "pf_default"


def snap(con_id: int, bid: float, ask: float, delta: float = -0.4) -> IbOptionSnapshot:
    return IbOptionSnapshot(
        con_id=con_id, bid=bid, ask=ask, last=None, time=IN_WINDOW, iv=0.3, delta=delta,
        gamma=0.03, vega=0.1, theta=-0.08, underlying_price=192.0,
    )  # fmt: skip


@pytest.fixture
def gw() -> FakeIbGateway:
    g = FakeIbGateway([ACCOUNT], contracts=(AAPL, P190, C210, P190_NOV, P185_NOV))
    g.set_values(NetLiquidation="100000", TotalCashValue="60000", AvailableFunds="60000")
    g.snapshot_data[265598] = IbSnapshot(265598, 192.0, 191.9, 192.1, IN_WINDOW)
    for con_id, bid, ask in ((1003, 1.9, 2.1), (1002, 0.05, 0.10), (1013, 4.0, 4.2),
                             (1014, 2.6, 2.8)):  # fmt: skip
        g.option_snapshot_data[con_id] = snap(con_id, bid, ask)
    return g


@pytest.fixture
def live_book(state):
    promote_to(state, PID, "live_small")
    set_approval(state, PID, "covered", actor="user:usr_owner", reason="test")
    return state


def broker_for(state, gw: FakeIbGateway, *, live: bool = True) -> IbkrBroker:
    return IbkrBroker(
        gw,
        mode="live",
        allow_live=True,
        options_gate=gate_lookup(state, PID, live=lambda: live),
        stage_lookup=lambda: "live_small",
    )


def run(state, gw, settings=ON, phase="plan", published=None):
    sink = published if published is not None else []
    return run_options_live(
        state,
        lambda pid: broker_for(state, gw),
        settings=settings,
        as_of=AS_OF if phase == "plan" else EXP,
        phase=phase,
        publish=sink.append,
        sessions_left=weekday_sessions,
        portfolio_ids=[PID],
    )


def submit(state, gw):
    return submit_tickets(state, lambda pid: broker_for(state, gw), clock=FakeClock(IN_WINDOW))


# ---- off by default ------------------------------------------------------------------------


def test_the_job_does_nothing_while_options_live_is_off(live_book, gw):
    gw.set_position(P190, -1)
    result = run(live_book, gw, settings=OptionsLiveSettings())
    assert not result.enabled and result.portfolios == ()
    assert list_tickets(live_book) == []


def test_live_portfolios_are_found_by_stage_or_approval(state):
    assert option_portfolios(state) == []
    set_approval(state, PID, "covered", actor="user:usr_owner", reason="test")
    assert option_portfolios(state) == [PID]


# ---- expiry closes ---------------------------------------------------------------------------


def test_a_short_put_expiring_tomorrow_gets_a_held_closing_ticket_at_the_mid(live_book, gw):
    gw.set_position(P190, -2)
    published: list = []
    result = run(live_book, gw, published=published)
    [pf] = result.portfolios
    assert (pf.status, pf.tickets, pf.awaiting_approval) == ("ok", 1, 1)
    [ticket] = list_tickets(live_book)
    assert (ticket.ticker, ticket.side, ticket.quantity) == (PUT_190, "buy", 2.0)
    assert ticket.status == "awaiting_approval" and ticket.hold == "options"
    assert ticket.limit_price == pytest.approx(2.0)  # the mid of 1.90 / 2.10
    assert ticket.order.position_effect == "close" and ticket.order.order_type == "limit"
    assert ticket.preview["notional"] == pytest.approx(400.0)  # 2 x 2.00 x 100
    assert "what_if" in ticket.preview
    assert any(e.title == "Option orders wait for you" for e in published)
    assert not gw.sent  # nothing reaches the broker before approval


def test_a_second_run_the_same_day_writes_no_second_ticket(live_book, gw):
    gw.set_position(P190, -2)
    run(live_book, gw)
    run(live_book, gw)
    assert len(list_tickets(live_book)) == 1


def test_an_approved_close_goes_out_as_a_day_limit_in_the_window(live_book, gw):
    gw.set_position(P190, -2)
    run(live_book, gw)
    [ticket] = list_tickets(live_book)
    decide_tickets(live_book, [ticket.id], approve=True, actor="user:usr_owner", now=IN_WINDOW)
    result = submit(live_book, gw)
    assert result.sent == 1, result
    contract, request = gw.sent[-1]
    assert contract.con_id == 1003
    assert (request.action, request.order_type, request.tif, request.limit_price) == (
        "BUY",
        "LMT",
        "DAY",
        2.0,
    )


def test_auto_approve_closes_lets_the_close_go_without_a_person(live_book, gw):
    gw.set_position(P190, -1)
    run(live_book, gw, settings=ON.model_copy(update={"auto_approve_closes": True}))
    [ticket] = list_tickets(live_book)
    assert ticket.status == "approved" and ticket.hold is None


def test_a_close_is_planned_even_when_the_switch_turned_off_later(live_book, gw):
    """Closing never needs the gate (P28): the broker lets it through."""
    gw.set_position(P190, -1)
    run(live_book, gw)
    [ticket] = list_tickets(live_book)
    decide_tickets(live_book, [ticket.id], approve=True, actor="user:usr_owner", now=IN_WINDOW)
    closed = submit_tickets(
        live_book,
        lambda pid: broker_for(live_book, gw, live=False),
        clock=FakeClock(IN_WINDOW),
    )
    assert closed.sent == 1


def test_a_short_with_no_quote_is_reported_not_guessed(live_book, gw):
    gw.set_position(P190, -1)
    del gw.option_snapshot_data[1003]
    published: list = []
    [pf] = run(live_book, gw, published=published).portfolios
    assert pf.tickets == 0 and pf.warnings
    assert any(e.title == "Option expiry needs you" for e in published)


def test_far_expiries_are_left_alone(live_book, gw):
    gw.set_position(P190_NOV, -1)
    [pf] = run(live_book, gw).portfolios
    assert pf.tickets == 0 and list_tickets(live_book) == []


# ---- rolls -----------------------------------------------------------------------------------


#: A rolled cash-secured put can lose its strike: allow it in these tests.
ROLL = ON.model_copy(
    update={
        "expiry": ExpirySettings(action="roll", roll_target_days=35),
        "max_loss_per_group": 0.5,
        "max_loss_total": 1.0,
    }
)


def test_the_default_max_loss_drops_a_roll_into_a_big_cash_secured_put(live_book, gw):
    gw.set_position(P190, -1)
    settings = ON.model_copy(update={"expiry": ExpirySettings(action="roll")})
    [pf] = run(live_book, gw, settings=settings).portfolios
    assert pf.tickets == 0 and "max loss" in next(iter(pf.dropped.values()))


def test_a_roll_is_one_combo_of_two_held_legs_sent_as_one_bag(live_book, gw):
    gw.set_position(P190, -1)
    [pf] = run(live_book, gw, settings=ROLL).portfolios
    assert pf.tickets == 2, pf
    tickets = sorted(list_tickets(live_book), key=lambda t: t.client_id)
    assert [(t.ticker, t.side, t.order.position_effect) for t in tickets] == [
        (PUT_190, "buy", "close"),
        (PUT_190_NOV, "sell", "open"),
    ]
    net = tickets[0].order.decision_context["net_limit"]
    assert net == pytest.approx(2.0 - 4.1)  # a credit: buy back at 2.00, sell at 4.10
    decide_tickets(
        live_book, [t.id for t in tickets], approve=True, actor="user:usr_owner", now=IN_WINDOW
    )
    result = submit(live_book, gw)
    assert result.sent == 2
    contract, request = gw.sent[-1]
    assert contract.sec_type == "BAG" and len(gw.sent) == 1
    assert [(leg.con_id, leg.action) for leg in contract.combo_legs] == [
        (1003, "BUY"),
        (1013, "SELL"),
    ]
    assert request.limit_price == pytest.approx(-2.1)
    combo = tickets[0].order.decision_context["combo_id"]
    gw.fill_combo(combo, 1, [2.0, 4.1])
    for t in tickets:
        state = broker_for(live_book, gw).get_order_state(t.client_id)
        assert state is not None and state.status == "filled"


def test_a_roll_waits_whole_while_one_leg_is_not_approved(live_book, gw):
    gw.set_position(P190, -1)
    run(live_book, gw, settings=ROLL)
    first = sorted(list_tickets(live_book), key=lambda t: t.client_id)[0]
    decide_tickets(live_book, [first.id], approve=True, actor="user:usr_owner", now=IN_WINDOW)
    result = submit(live_book, gw)
    assert result.sent == 0 and not gw.sent


def test_a_roll_needs_the_gate_open(live_book, gw):
    set_approval(live_book, PID, "none", actor="user:usr_owner", reason="stop")
    gw.set_position(P190, -1)
    [pf] = run(live_book, gw, settings=ROLL).portfolios
    assert pf.tickets == 0 and "approval level is none" in next(iter(pf.dropped.values()))


# ---- assignments and the expiry-day watch -------------------------------------------------


def test_an_assignment_is_booked_once_into_the_ledger(live_book, gw):
    gw.option_event(P190, "assignment", -2, event_id="E1")
    published: list = []
    [pf] = run(live_book, gw, published=published).portfolios
    assert pf.events_booked == 1
    run(live_book, gw)
    fills = live_book.sql(
        "SELECT f.ticker, f.quantity, f.price, o.side FROM fills f"
        " JOIN orders o ON o.client_id = f.order_client_id ORDER BY f.ticker"
    )
    assert [(r["ticker"], r["quantity"], r["price"], r["side"]) for r in fills] == [
        ("AAPL.US", 200.0, 190.0, "buy"),  # a short put assigned buys the shares
        (PUT_190, 2.0, 0.0, "buy"),
    ]
    assert any(e.title == "Option assigned or exercised" for e in published)
    rows = live_book.sql("SELECT kind, shares FROM option_events")
    assert [(r["kind"], r["shares"]) for r in rows] == [("assignment", 200.0)]


def test_the_expiry_watch_alerts_on_a_short_in_the_money_today(live_book, gw):
    gw.set_position(P190, -1)
    gw.snapshot_data[265598] = IbSnapshot(265598, 185.0, 184.9, 185.1, IN_WINDOW)
    published: list = []
    [pf] = run(live_book, gw, phase="watch", published=published).portfolios
    assert pf.warnings and "in the money" in pf.warnings[0]
    [event] = published
    assert event.urgency == "high" and "AAPL" not in event.body  # no tickers leave the server
    assert not gw.sent


def test_the_expiry_watch_is_quiet_for_a_short_far_out_of_the_money(live_book, gw):
    gw.set_position(C210, -1)
    gw.snapshot_data[265598] = IbSnapshot(265598, 192.0, 191.9, 192.1, IN_WINDOW)
    published: list = []
    [pf] = run(live_book, gw, phase="watch", published=published).portfolios
    assert pf.warnings == () and published == []


# ---- the scheduled job -----------------------------------------------------------------------


def _job_ctx(settings, phase: str):
    from stonks.scheduling.jobs import JobSpec, RunContext
    from stonks.scheduling.triggers import Fire, SessionTrigger

    at = datetime(2026, 10, 15, 20, 55, tzinfo=UTC)
    return RunContext(
        spec=JobSpec(
            "options_live",
            "options_live",
            SessionTrigger("XNYS", "close", timedelta(0)),
            params={"phase": phase},
        ),
        fire=Fire(at, AS_OF, "k"),
        run_id="srun_t",
        now=at,
        settings=settings,
        notifier=None,  # type: ignore[arg-type]
    )


def _settings(tmp_path, **production):
    from stonks.config import Settings

    return Settings(
        state={"path": tmp_path / "state.sqlite"},
        lake={"path": tmp_path / "lake.duckdb"},
        notify={"backends": []},
        production=production,
    )


@pytest.mark.parametrize("phase", ["plan", "watch"])
def test_the_job_skips_on_every_backend_while_options_live_is_off(tmp_path, phase):
    from stonks.scheduling.api_backend import API_ACTIONS
    from stonks.scheduling.in_process import IN_PROCESS_ACTIONS
    from stonks.scheduling.local import LOCAL_ACTIONS

    settings = _settings(tmp_path)
    for registry in (LOCAL_ACTIONS, API_ACTIONS, IN_PROCESS_ACTIONS):
        out = registry.get("options_live")(_job_ctx(settings, phase))
        assert out.status == "skipped" and out.detail["reason"] == "options_live_off"


def test_the_job_skips_when_no_portfolio_trades_options(tmp_path):
    from stonks.scheduling.local import LOCAL_ACTIONS
    from stonks.store.state import SqliteState

    settings = _settings(tmp_path, options={"live": True})
    with SqliteState(settings.state.path) as st:
        st.migrate()
    out = LOCAL_ACTIONS.get("options_live")(_job_ctx(settings, "plan"))
    assert out.status == "skipped" and out.detail["reason"] == "no_option_portfolios"


def test_an_event_whose_fills_failed_to_book_is_booked_on_the_next_run(state, monkeypatch):
    """The event row, its orders and its fills land together: a run that
    dies before the fills are booked leaves nothing, so the next run books
    the event in full instead of skipping it as known."""
    import stonks.execution.reconcile as reconcile
    from stonks.options.live.events import OptionEvent, book_events

    event = OptionEvent("E9", "assignment", PUT_190, -2.0, EXP)
    real = reconcile.book_executions

    def crash(*args, **kwargs):
        raise RuntimeError("the process died here")

    monkeypatch.setattr(reconcile, "book_executions", crash)
    with pytest.raises(RuntimeError):
        book_events(state, PID, [event])
    monkeypatch.setattr(reconcile, "book_executions", real)

    assert len(book_events(state, PID, [event])) == 1
    fills = state.sql("SELECT ticker, quantity FROM fills ORDER BY ticker")
    assert [(r["ticker"], r["quantity"]) for r in fills] == [("AAPL.US", 200.0), (PUT_190, 2.0)]
