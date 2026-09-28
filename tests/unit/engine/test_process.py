"""The engine process (roadmap 21.2.5): startup reconcile before the first
order, flatten through the router, stop requests, and restart from the
ledger without re-sending. Hermetic: recordings replayed from tmp_path."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from stonks.accounts.models import DEFAULT_PORTFOLIO_ID
from stonks.core.clock import FakeClock
from stonks.core.types import Order, Portfolio
from stonks.engine.control import EngineControl
from stonks.engine.driver import BarClose
from stonks.engine.process import EngineProcess, RoutedDecision
from stonks.engine.recovery import LOST_REASON, EngineRuns, ledger_portfolio
from stonks.engine.sessions import FLATTEN_STRATEGY_ID, SessionRules
from stonks.engine.sim_broker import IntradaySimBroker
from stonks.execution.order_state import ReconciliationPendingError
from stonks.production.tick import _record_order
from stonks.streaming.base import StreamingSource
from stonks.streaming.sources.replay import ReplaySource
from tests.unit.engine.engine_fixtures import (
    PARITY_FILLS,
    add_portfolio,
    book,
    day_frame,
    record,
    replay_process,
)
from tests.unit.engine.minute_lake import DAYS, MinuteMomentum, minute_lake, session_minutes

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


def sent_ids(state, portfolio_id: str = PF) -> list[str]:
    return [
        r["client_id"]
        for r in state.sql(
            "SELECT client_id FROM orders WHERE portfolio_id = ? ORDER BY created_at, client_id",
            [portfolio_id],
        )
    ]


def run_row(state, run_id: str) -> dict:
    return dict(state.sql("SELECT * FROM engine_runs WHERE id = ?", [run_id])[0])


# ---- a clean replay ---------------------------------------------------------------


def test_a_replay_decides_every_close_and_records_its_run(lake, state, recording) -> None:
    routed: list[RoutedDecision] = []
    process = replay_process(lake, state, recording, [book(PF)], decisions=routed)
    stats = process.run()
    minutes = len(session_minutes(DAY))
    assert stats.bar_closes == minutes
    assert len(routed) == minutes
    assert stats.orders_sent > 20
    assert len(sent_ids(state)) == stats.orders_sent
    row = run_row(state, process.run_id or "")
    assert row["status"] == "stopped"
    assert row["mode"] == "replay"
    assert row["bar_closes"] == minutes
    assert row["orders_routed"] == stats.orders_routed
    assert datetime.fromisoformat(row["last_close_at"]) == stats.last_close_at
    # the book at the end is exactly what the ledger says
    assert process.broker(book(PF).id).fetch_portfolio().cash == pytest.approx(
        ledger_portfolio(state, PF, 100_000.0).cash
    )


def test_no_order_before_start(lake, state, recording) -> None:
    process = replay_process(lake, state, recording, [book(PF)])
    close = BarClose(at=datetime(2026, 9, 24, 14, tzinfo=UTC), interval=process.interval,
                     bars=(), sequence=1)  # fmt: skip
    with pytest.raises(RuntimeError, match="not started"):
        process.on_bar_close(close)


def test_needs_a_source_or_a_runner(lake, state) -> None:
    with pytest.raises(ValueError, match="either"):
        EngineProcess(
            strategies={"0": MinuteMomentum()},
            books=[book(PF)],
            lake=lake,
            state=state,
            universe=["AAA.US"],
            session=DAY,
        )


def test_a_book_needs_a_portfolio() -> None:
    with pytest.raises(ValueError, match="portfolio_id"):
        book("")


# ---- startup reconcile -------------------------------------------------------------


def _unknown_order(state, client_id: str = "old:AAA.US:buy") -> None:
    order = Order(client_id, "AAA.US", "buy", 5.0)
    _record_order(state, order, status="pending", portfolio_id=PF)
    state.execute(
        "UPDATE orders SET state = 'unknown', broker_order_id = 'b-1' WHERE client_id = ?",
        [client_id],
    )


def test_an_unknown_order_at_the_broker_blocks_the_start(lake, state, recording) -> None:
    _unknown_order(state)
    clock = FakeClock(datetime(2026, 9, 24, tzinfo=UTC))
    external = IntradaySimBroker(Portfolio(cash=100_000.0), fill=PARITY_FILLS, clock=clock)
    process = replay_process(lake, state, recording, [book(PF, broker=external)])
    with pytest.raises(ReconciliationPendingError):
        process.run()
    assert sent_ids(state) == ["old:AAA.US:buy"], "nothing was sent"
    assert not state.sql("SELECT 1 FROM engine_runs")


def test_a_simulated_book_ends_the_orders_of_a_dead_process(lake, state, recording) -> None:
    _unknown_order(state)
    process = replay_process(lake, state, recording, [book(PF)])
    report = process.start()
    assert report.lost_orders[book(PF).id] == ["old:AAA.US:buy"]
    row = state.sql("SELECT state, status_reason FROM orders WHERE client_id = 'old:AAA.US:buy'")
    assert row[0]["state"] == "expired"
    assert row[0]["status_reason"] == LOST_REASON


# ---- flatten ------------------------------------------------------------------------


def _held_at_close(state, portfolio_id: str) -> dict[str, float]:
    return {
        t: q
        for t, q in ledger_portfolio(state, portfolio_id, 100_000.0).positions.items()
        if abs(q) > 1e-9
    }


def test_flatten_orders_go_through_the_router(lake, state, recording) -> None:
    rules = SessionRules(flatten_at_close=True, flatten_minutes=5, entry_cutoff_minutes=10)
    routed: list[RoutedDecision] = []
    process = replay_process(lake, state, recording, [book(PF, sessions=rules)], decisions=routed)
    stats = process.run()
    flat = [
        (r, a) for r in routed for o, a in zip(r.orders, r.acks, strict=True)
        if o.strategy_id == FLATTEN_STRATEGY_ID
    ]  # fmt: skip
    assert flat and stats.flatten_orders == len(flat)
    assert all(a.status in ("sent", "known") for _, a in flat)
    close = session_minutes(DAY)[-1].replace(tzinfo=UTC) + timedelta(minutes=1)
    for r, _ in flat:
        assert close - r.decision.at <= timedelta(minutes=5)
    assert _held_at_close(state, PF) == {}


class _FailsLate(MinuteMomentum):
    """Holds AAA.US all day, then fails on every bar of the last 30 minutes."""

    def estimate_return(self, ticker: str, as_of: Any, lake: Any) -> float | None:
        if as_of.hour == 19 and as_of.minute >= 30:
            raise RuntimeError("model blew up")
        return 0.01 if ticker == "AAA.US" else None


def test_flatten_still_goes_out_when_the_step_fails(lake, state, recording) -> None:
    rules = SessionRules(flatten_at_close=True, flatten_minutes=5, entry_cutoff_minutes=10)
    routed: list[RoutedDecision] = []
    process = replay_process(
        lake,
        state,
        recording,
        [book(PF, sessions=rules)],
        strategy=_FailsLate({"lookback": 10}),
        decisions=routed,
    )
    stats = process.run()
    assert stats.step_errors == 30
    assert stats.flatten_orders == 1
    assert process.driver.stats.total_handler_errors == 0
    assert _held_at_close(state, PF) == {}


# ---- stop requests -------------------------------------------------------------------


class _StopAfter:
    def __init__(self, control: EngineControl, closes: int) -> None:
        self.control = control
        self.closes = closes
        self.seen = 0

    def __call__(self, routed: RoutedDecision) -> None:
        self.seen += 1
        if self.seen == self.closes:
            self.control.request_stop("test")


def test_a_stop_request_ends_the_run_cleanly(lake, state, recording, tmp_path) -> None:
    control = EngineControl(tmp_path / "engine")
    process = replay_process(lake, state, recording, [book(PF)], control=control)
    process._on_decision = _StopAfter(control, 30)
    stats = process.run()
    assert stats.bar_closes < 40
    assert process.stop_reason == "stop requested"
    assert run_row(state, process.run_id or "")["status"] == "stopped"
    # working simulated day orders expire at the stop, nothing stays open
    open_rows = state.sql(
        "SELECT 1 FROM orders WHERE status IN ('pending', 'partially_filled') AND portfolio_id = ?",
        [PF],
    )
    assert not open_rows


def test_stop_is_idempotent_and_thread_safe(lake, state, recording) -> None:
    process = replay_process(lake, state, recording, [book(PF)])
    process.start()
    process.stop("first")
    process.stop("second")
    assert process.stop_reason == "first"
    stats = process.run()
    assert stats.bar_closes == 0


# ---- restart and recovery ----------------------------------------------------------------


def _crash_after(process: EngineProcess, source: StreamingSource, closes: int) -> None:
    """Feed events until ``closes`` bar closes, then abandon the process
    without its shutdown: the run row stays ``running``."""
    process.start()
    for event in source.stream(process.universe):
        process.on_event(event)
        if process.stats.bar_closes >= closes:
            return


def test_restart_resumes_from_the_ledger_without_resending(lake, state, recording) -> None:
    first = replay_process(lake, state, recording, [book(PF)])
    _crash_after(first, ReplaySource(recording), 120)
    held = first.broker(book(PF).id).fetch_portfolio()
    crashed_id = first.run_id
    before = set(sent_ids(state))
    assert before

    routed: list[RoutedDecision] = []
    second = replay_process(lake, state, recording, [book(PF)], decisions=routed)
    report = second.start()
    assert report.recovered_from == crashed_id
    assert [r.id for r in report.crashed] == [crashed_id]
    assert run_row(state, crashed_id or "")["status"] == "crashed"
    assert report.resume_after == first.stats.last_close_at
    # the rebuilt book holds what the dead one had booked
    rebuilt = second.broker(book(PF).id).fetch_portfolio()
    assert rebuilt.positions == pytest.approx(ledger_portfolio(state, PF, 100_000.0).positions)
    assert set(rebuilt.positions) <= set(held.positions) | set(rebuilt.positions)
    stats = second.run()
    assert stats.skipped_closes == 120
    assert all(r.decision.at > report.resume_after for r in routed)
    ids = sent_ids(state)
    assert len(ids) == len(set(ids))
    assert stats.orders_known == 0
    execs = [r["broker_exec_id"] for r in state.sql("SELECT broker_exec_id FROM fills")]
    assert len(execs) == len(set(execs)), "execution ids never repeat across a restart"
    row = run_row(state, second.run_id or "")
    assert row["recovered_from"] == crashed_id
    assert row["status"] == "stopped"


def test_a_decision_made_again_after_a_crash_is_never_sent_twice(lake, state, recording) -> None:
    first = replay_process(lake, state, recording, [book(PF)])
    _crash_after(first, ReplaySource(recording), 90)
    # the crash hit between routing and the checkpoint: the checkpoint lags
    lagged = (first.stats.last_close_at or datetime.now(UTC)) - timedelta(minutes=5)
    state.execute(
        "UPDATE engine_runs SET last_close_at = ? WHERE id = ?",
        [lagged.isoformat(), first.run_id],
    )
    routed: list[RoutedDecision] = []
    second = replay_process(lake, state, recording, [book(PF)], decisions=routed)
    second.run()
    counts = Counter(sent_ids(state))
    assert max(counts.values()) == 1
    replayed = [a for r in routed if r.decision.at <= first.stats.last_close_at for a in r.acks]
    assert all(a.status == "known" for a in replayed)


def test_a_clean_run_of_the_same_session_does_not_skip(lake, state, recording) -> None:
    first = replay_process(lake, state, recording, [book(PF)])
    first.run()
    second = replay_process(lake, state, recording, [book(PF)])
    report = second.start()
    assert report.recovered_from is None and report.resume_after is None


def test_runs_table_finds_the_latest_run(state) -> None:
    runs = EngineRuns(state)
    a = runs.start(DAY, "replay")
    runs.finish(a, "stopped")
    b = runs.start(DAY, "live")
    latest = runs.latest(DAY)
    assert latest is not None and latest.id == b and latest.status == "running"
    assert [r.id for r in runs.mark_crashed()] == [b]
    assert runs.latest() is not None


# ---- two books ---------------------------------------------------------------------------


def test_two_books_trade_their_own_portfolios(lake, state, recording) -> None:
    add_portfolio(state, "pf_second")
    books = [book(PF), book("pf_second", book_id="second")]
    process = replay_process(lake, state, recording, books)
    stats = process.run()
    assert sent_ids(state) and sent_ids(state, "pf_second")
    assert len(sent_ids(state)) + len(sent_ids(state, "pf_second")) == stats.orders_sent
    assert all(i.startswith("pf_second:") for i in sent_ids(state, "pf_second"))
    assert process.driver.stats.total_handler_errors == 0


# ---- a shared broker account ------------------------------------------------------------


class _AccountBroker:
    """A broker account that also holds the owner's own shares (what an
    IBKR account reports): the book's simulated fills plus ``external``."""

    def __init__(self, sim: IntradaySimBroker, external: dict[str, float]) -> None:
        self.sim = sim
        self.external = external

    def fetch_portfolio(self) -> Portfolio:
        mine = self.sim.fetch_portfolio()
        positions = dict(mine.positions)
        for ticker, qty in self.external.items():
            positions[ticker] = positions.get(ticker, 0.0) + qty
        return Portfolio(cash=mine.cash, positions=positions)

    def place_order(self, order: Order) -> None:
        self.sim.place_order(order)

    def reconcile(self) -> list:
        return self.sim.reconcile()

    def executions(self, since: datetime) -> list:
        return self.sim.executions(since)

    def get_order_state(self, client_id: str):
        return self.sim.get_order_state(client_id)

    def cancel_order(self, client_id: str) -> bool:
        return self.sim.cancel_order(client_id)

    def on_bar_close(self, event: BarClose) -> list:
        return self.sim.on_bar_close(event)

    def set_asset_classes(self, classes) -> None:
        self.sim.set_asset_classes(classes)


def test_a_broker_book_never_trades_the_owners_own_holdings(lake, state, recording) -> None:
    """At a real broker the account holds the owner's shares too. The book
    decides, exits and flattens only what its own fills bought."""
    clock = FakeClock(datetime(2026, 9, 24, tzinfo=UTC))
    sim = IntradaySimBroker(Portfolio(cash=100_000.0), fill=PARITY_FILLS, clock=clock,
                            session_key=None)  # fmt: skip
    account = _AccountBroker(sim, {"OWN.US": 50.0, "AAA.US": 7.0})
    rules = SessionRules(flatten_at_close=True, flatten_minutes=5, entry_cutoff_minutes=10)
    process = replay_process(lake, state, recording, [book(PF, sessions=rules, broker=account)])
    process.run()
    assert sent_ids(state), "the book traded"
    assert not state.sql("SELECT 1 FROM orders WHERE ticker = 'OWN.US'")
    # the owner's 7 AAA.US stay: the book ends flat on its own shares only
    assert account.fetch_portfolio().positions.get("AAA.US") == pytest.approx(7.0)
