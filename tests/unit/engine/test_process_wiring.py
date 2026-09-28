"""The engine process wired to the rest of Phase 21: the monitor (21.3.4),
intraday risk and halts on every event (21.3.2), intraday P&L (21.3.3) and
the decision interval intraday TCA selects orders by (21.3.5). Hermetic:
one recorded session replayed from tmp_path."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from stonks.accounts.models import DEFAULT_PORTFOLIO_ID
from stonks.config import RiskPolicy
from stonks.engine.status import EngineStatusStore
from stonks.production.halts import active_halts, trip_halt
from stonks.production.intraday_pnl import IntradayPnlTracker, list_intraday_snapshots
from stonks.production.intraday_pnl_settings import IntradayPnlSettings
from stonks.production.intraday_tca import is_intraday_order
from stonks.production.rules.intraday_loss import IntradayLossLimitSettings
from stonks.production.rules.intraday_orders import IntradayOrderRateSettings
from stonks.production.rules.settings import RuleSettings
from tests.unit.engine.engine_fixtures import book, day_frame, record, replay_process
from tests.unit.engine.minute_lake import DAYS, minute_lake, session_minutes

PF = DEFAULT_PORTFOLIO_ID
DAY = DAYS[0]


@pytest.fixture(scope="module")
def lake():
    lk = minute_lake()
    yield lk
    lk.close()


@pytest.fixture(scope="module")
def recording(tmp_path_factory):
    return record(day_frame(DAY), tmp_path_factory.mktemp("rec"))


def _orders(state) -> list[dict]:
    return [dict(r) for r in state.sql("SELECT * FROM orders WHERE portfolio_id = ?", [PF])]


def _risk_book(**rules):
    b = book(PF)
    policy = RiskPolicy(rules=RuleSettings(**rules))
    b.spec = replace(b.spec, risk=policy)
    return b


# ---- the monitor (21.3.4) ----------------------------------------------------------


def test_the_engine_publishes_its_status_and_event_to_order_latency(
    lake, state, recording, tmp_path
) -> None:
    store = EngineStatusStore(tmp_path / "state.sqlite")
    process = replay_process(
        lake, state, recording, [book(PF)], status=store, engine_id="intraday", calendar="XNYS"
    )
    stats = process.run()
    [row] = store.read_all()
    assert row.engine_id == "intraday"
    assert row.calendar == "XNYS"
    assert row.stopped_at is not None
    assert row.driver["bar_closes"] == len(session_minutes(DAY))
    # every order the router sent was timed from its bar close
    assert stats.orders_sent > 0
    assert row.latency("event_to_order").count == stats.orders_sent
    assert process.monitor.event_to_order.count == stats.orders_sent


def test_without_a_store_the_monitor_still_measures(lake, state, recording) -> None:
    process = replay_process(lake, state, recording, [book(PF)])
    stats = process.run()
    assert process.monitor.dispatch_lag.count == stats.bar_closes
    assert process.monitor.event_to_order.count == stats.orders_sent


# ---- the decision interval (21.3.5) ------------------------------------------------


def test_every_order_carries_its_decision_interval(lake, state, recording) -> None:
    replay_process(lake, state, recording, [book(PF)]).run()
    rows = _orders(state)
    assert rows
    for row in rows:
        assert json.loads(row["decision_context_json"])["interval"] == "1m"
        assert is_intraday_order(row)


# ---- halts on every event (21.3.2) -------------------------------------------------


def test_a_stop_all_kill_switch_stops_every_order(lake, state, recording) -> None:
    trip_halt(state, "kill", reason="drill", actor="user:usr_owner", scope="global", halt="all")
    process = replay_process(lake, state, recording, [book(PF)])
    stats = process.run()
    assert stats.orders_sent == 0
    assert stats.orders_halted > 0
    assert _orders(state) == []


def test_a_buys_halt_keeps_closes_only(lake, state, recording) -> None:
    trip_halt(state, "kill", reason="buys off", actor="user:usr_owner", portfolio_id=PF)
    process = replay_process(lake, state, recording, [book(PF)])
    stats = process.run()
    assert stats.orders_halted > 0
    # a flat book has nothing to close, so nothing goes out
    assert all(r["side"] == "sell" for r in _orders(state))


def test_a_kill_switch_pressed_mid_session_stops_the_next_order(lake, state, recording) -> None:
    process = replay_process(lake, state, recording, [book(PF)])
    process.start()
    fired = {"done": False}
    original = process._after_marks

    def press_after(event):
        if not fired["done"] and process.stats.orders_sent > 0:
            trip_halt(state, "kill", reason="now", actor="user:usr_owner", portfolio_id=PF,
                      halt="all")  # fmt: skip
            fired["done"] = True
            sent_before["n"] = process.stats.orders_sent
        original(event)

    sent_before = {"n": 0}
    process._after_marks = press_after  # type: ignore[method-assign]
    stats = process.run()
    assert fired["done"]
    assert stats.orders_sent == sent_before["n"]
    assert stats.orders_halted > 0


def test_an_intraday_loss_opens_the_halt(lake, state, recording) -> None:
    b = _risk_book(intraday_loss_limit=IntradayLossLimitSettings(max_loss=0.00001))
    process = replay_process(lake, state, recording, [b], notify_halts=False)
    stats = process.run()
    halts = [h for h in active_halts(state, DAY, portfolio_id=PF) if h.kind == "intraday_loss"]
    assert len(halts) == 1
    assert stats.halts_tripped >= 1


def test_an_order_burst_opens_the_runaway_halt(lake, state, recording) -> None:
    b = _risk_book(intraday_order_rate=IntradayOrderRateSettings(max_orders_per_day=1))
    process = replay_process(lake, state, recording, [b], notify_halts=False)
    process.run()
    kinds = {h.kind for h in active_halts(state, DAY, portfolio_id=PF)}
    assert "runaway" in kinds
    opens = [r for r in _orders(state) if r["side"] == "buy"]
    assert len(opens) <= 1


# ---- intraday P&L (21.3.3) ---------------------------------------------------------


def test_the_engine_stores_intraday_pnl(lake, state, recording) -> None:
    tracker = IntradayPnlTracker(state, [PF], settings=IntradayPnlSettings(enabled=True))
    process = replay_process(lake, state, recording, [book(PF)], pnl=tracker)
    process.run()
    snaps, total = list_intraday_snapshots(state, PF, DAY, limit=500)
    minutes = len(session_minutes(DAY))
    assert total >= minutes // 5
    assert snaps[0].fills > 0
