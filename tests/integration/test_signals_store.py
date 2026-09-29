"""The stored signal phase (S5): every scored strategy's signals and signal
events, once per tick for everyone, with a plain reason each."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date

import pytest

from stonks.accounts import Mode, Role, Scope, SubscriptionRepository, UserRepository
from stonks.core.protocols import SurvivalReport
from stonks.production import signals as signals_mod
from stonks.production.signals import events_for, record_signals
from stonks.production.tick import TickSettings, load_tick_plan, run_tick
from stonks.registry.store import StrategyRegistry
from stonks.store.state import SqliteState
from stonks.strategies.examples.buy_and_hold import BuyAndHold
from stonks.strategies.examples.momentum import Momentum
from tests.fixtures.governance import seed_status

DAY1, DAY2, DAY3 = date(2026, 3, 17), date(2026, 3, 18), date(2026, 3, 19)
UNIVERSE = ["UP.US", "FLAT.US", "DOWN.US"]
SETTINGS = TickSettings(universe=UNIVERSE, initial_cash=10_000.0)


class Explained(BuyAndHold):
    """A strategy with an ``explain`` hook (module scope for the registry)."""

    def explain(self, ticker, as_of, lake):
        return {"text": f"{ticker} is the one asset this benchmark holds.", "held_since": "day 1"}


class FirstDayOnly(BuyAndHold):
    """Likes its ticker on DAY1 only (module scope for the registry)."""

    def estimate_return(self, ticker, as_of, lake):
        day = as_of.date() if hasattr(as_of, "date") else as_of
        return super().estimate_return(ticker, as_of, lake) if day <= DAY1 else None


class BadlyExplained(BuyAndHold):
    def explain(self, ticker, as_of, lake):
        raise RuntimeError("no words")


@pytest.fixture
def env(tmp_path, lake_trending):
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    registry = StrategyRegistry(state=state, artifacts_dir=tmp_path / "artifacts")
    reports = [SurvivalReport(test_id="oos", passed=True, metrics={})]
    for sid, strategy, status in [
        ("mom", Momentum({"lookback_days": 5, "threshold": 0.0, "allocation": 0.5}), "active"),
        ("bh_flat", Explained({"ticker": "FLAT.US", "allocation": 0.5}), "shadow"),
        ("bh_down", BadlyExplained({"ticker": "DOWN.US", "allocation": 0.4}), "shadow"),
    ]:
        registry.register(strategy, reports=reports, strategy_id=sid)
        if status != "shadow":
            seed_status(registry, sid, status)
    yield lake_trending, state, registry
    state.close()


def _signals(state, day):
    rows = state.sql(
        "SELECT strategy_id, ticker, score, rank, model_weight FROM signals WHERE as_of = ?"
        " ORDER BY strategy_id, ticker",
        [day.isoformat()],
    )
    return [dict(r) for r in rows]


def _events(state, day):
    return {
        (e.strategy_id, e.ticker, e.kind): e
        for e in events_for(state, day, ["mom", "bh_flat", "bh_down"])
    }


def test_every_scored_strategy_is_recorded_once_with_reasons(env):
    lake, state, registry = env
    run_tick(state, lake, registry, SETTINGS, as_of=DAY1)

    rows = {(r["strategy_id"], r["ticker"]): r for r in _signals(state, DAY1)}
    # active without a model book: scores and ranks, no model weight
    mom = [r for (sid, _), r in rows.items() if sid == "mom"]
    assert mom and all(r["model_weight"] is None and r["score"] > 0 for r in mom)
    assert sorted(r["rank"] for r in mom) == list(range(1, len(mom) + 1))
    # shadow with a model book: the book's weight
    assert rows[("bh_flat", "FLAT.US")]["model_weight"] == pytest.approx(0.5, abs=0.02)

    events = _events(state, DAY1)
    flat = events[("bh_flat", "FLAT.US", "entry")]
    assert flat.text == "FLAT.US is the one asset this benchmark holds."  # explain() wins
    assert flat.reason["held_since"] == "day 1" and flat.reason["source"] == "model_book"
    assert flat.strength == pytest.approx(0.5, abs=0.02)
    down = events[("bh_down", "DOWN.US", "entry")]
    assert down.reason["explain_error"] == "RuntimeError: no words"
    # Vocabulary: a test book, and no "expected return" read off a book's score.
    assert down.text.startswith("The test book bought DOWN.US")
    assert "expected return" not in down.text and "rank 1 of" in down.text
    [up] = [e for (sid, t, k), e in events.items() if sid == "mom" and t == "UP.US"]
    assert up.kind == "entry" and up.reason["source"] == "scores"
    assert up.text.startswith("mom now scores UP.US: expected return")

    count = state.count_rows("signal_events")
    run_tick(state, lake, registry, SETTINGS, as_of=DAY1)  # a re-run writes nothing twice
    assert state.count_rows("signal_events") == count
    assert len(_signals(state, DAY1)) == len(rows)


def test_unchanged_days_have_no_events_and_dropped_scores_exit(env):
    lake, state, registry = env
    run_tick(state, lake, registry, SETTINGS, as_of=DAY1)
    run_tick(state, lake, registry, SETTINGS, as_of=DAY2)
    assert _events(state, DAY2) == {}
    run_tick(state, lake, registry, replace(SETTINGS, threshold=2.0), as_of=DAY3)
    kinds = {(sid, k) for (sid, _, k) in _events(state, DAY3)}
    assert ("mom", "exit") in kinds


def test_model_books_for_every_strategy_turn_active_signals_into_book_moves(env):
    lake, state, registry = env
    run_tick(state, lake, registry, replace(SETTINGS, model_books="all"), as_of=DAY1)
    mom = [r for r in _signals(state, DAY1) if r["strategy_id"] == "mom"]
    assert all(r["model_weight"] is not None for r in mom)
    mom_events = [e for (sid, _, _), e in _events(state, DAY1).items() if sid == "mom"]
    assert mom_events and all(e.reason["source"] == "model_book" for e in mom_events)


def test_a_dry_run_records_nothing(env):
    lake, state, registry = env
    run_tick(state, lake, registry, SETTINGS, as_of=DAY1, dry_run=True)
    assert state.count_rows("signals") == 0 and state.count_rows("signal_events") == 0


def test_a_failing_signal_phase_never_fails_the_tick(env, monkeypatch):
    lake, state, registry = env

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr("stonks.production.tick.record_signals", boom)
    assert run_tick(state, lake, registry, SETTINGS, as_of=DAY1).status == "ok"


def test_notify_subscribers_get_the_stored_events_with_their_reason(env):
    lake, state, registry = env
    carol = Scope.for_user(
        UserRepository(state).create(display_name="C", role=Role.TRADER, actor="t")
    )
    SubscriptionRepository(state).subscribe(carol, strategy_id="bh_flat", mode=Mode.NOTIFY)
    plan = load_tick_plan(state, SETTINGS)
    run_tick(state, lake, registry, SETTINGS, as_of=DAY1, plan=plan)
    [row] = state.sql("SELECT title, body FROM notification_outbox WHERE category = 'signal'")
    assert row["title"] == "FLAT.US: entry signal"
    assert "FLAT.US is the one asset this benchmark holds." in row["body"]


def test_record_signals_skips_strategies_already_recorded(env):
    lake, state, registry = env
    first = record_signals(state, lake, {"x": {"UP.US": 0.1}}, {}, tick_id="t1", as_of=DAY1)
    assert [e.kind for e in first["x"]] == ["entry"]
    assert record_signals(state, lake, {"x": {"UP.US": 0.9}}, {}, tick_id="t2", as_of=DAY1) == {}
    [row] = state.sql("SELECT tick_id, score FROM signals WHERE strategy_id = 'x'")
    assert (row["tick_id"], row["score"]) == ("t1", 0.1)
    assert json.loads(state.sql("SELECT reason_json FROM signal_events")[0][0])["text"].startswith(
        "x now scores UP.US"
    )
    assert signals_mod.events_for(state, DAY1, []) == []


# ---- BE-16, BE-17: exits reach notify subscribers, once ------------------------------------


def test_be17_an_exit_is_written_once_across_empty_days(env):
    lake, state, _ = env
    days = [DAY1, DAY2, DAY3, date(2026, 3, 20)]
    for i, (day, scores) in enumerate(zip(days, [{"A.US": 0.1}, {}, {}, {}], strict=True)):
        record_signals(state, lake, {"s1": scores}, {}, tick_id=f"t{i}", as_of=day)
    kinds = [(d, e.ticker, e.kind) for d in days for e in events_for(state, d, ["s1"])]
    assert kinds == [(DAY1, "A.US", "entry"), (DAY2, "A.US", "exit")]
    # a re-run of an empty day writes nothing twice
    assert record_signals(state, lake, {"s1": {}}, {}, tick_id="t9", as_of=DAY3) == {}


def test_be16_notify_subscribers_get_the_exit_notice(env):
    lake, state, registry = env
    reports = [SurvivalReport(test_id="oos", passed=True, metrics={})]
    registry.register(
        FirstDayOnly({"ticker": "UP.US", "allocation": 0.5}), reports=reports, strategy_id="once"
    )
    seed_status(registry, "once", "active")
    carol = Scope.for_user(
        UserRepository(state).create(display_name="C", role=Role.TRADER, actor="t")
    )
    SubscriptionRepository(state).subscribe(carol, strategy_id="once", mode=Mode.NOTIFY)
    for day in (DAY1, DAY2):
        plan = load_tick_plan(state, SETTINGS)
        run_tick(state, lake, registry, SETTINGS, as_of=day, plan=plan)
    titles = [
        r["title"]
        for r in state.sql(
            "SELECT title FROM notification_outbox WHERE category = 'signal' ORDER BY id"
        )
    ]
    assert titles == ["UP.US: entry signal", "UP.US: exit signal"]


# ---- next-open fills: a signal is sent on the day the book decides -----------------


NEXT_OPEN = replace(SETTINGS, paper_fills="next_open")


def test_a_next_open_book_signals_on_the_day_it_decides(env):
    """With ``paper_fills = "next_open"`` (the shipped default) the test
    book's order waits for the next open. Its follower must hear about it
    on the day it decides, not a trading run later, and only once."""
    lake, state, registry = env
    carol = Scope.for_user(
        UserRepository(state).create(display_name="C", role=Role.TRADER, actor="t")
    )
    SubscriptionRepository(state).subscribe(carol, strategy_id="bh_flat", mode=Mode.NOTIFY)
    run_tick(state, lake, registry, NEXT_OPEN, as_of=DAY1, plan=load_tick_plan(state, NEXT_OPEN))

    flat = _events(state, DAY1)[("bh_flat", "FLAT.US", "entry")]
    assert flat.strength == pytest.approx(0.5, abs=0.02)
    down = _events(state, DAY1)[("bh_down", "DOWN.US", "entry")]
    assert down.text.startswith("The test book buys DOWN.US at the next open")
    [row] = state.sql("SELECT title FROM notification_outbox WHERE category = 'signal'")
    assert row["title"] == "FLAT.US: entry signal"

    # The next run fills the order at the open: nothing new to say.
    run_tick(state, lake, registry, NEXT_OPEN, as_of=DAY2, plan=load_tick_plan(state, NEXT_OPEN))
    assert [k for (sid, _, k) in _events(state, DAY2) if sid == "bh_flat"] == []
    assert state.count_rows("notification_outbox") == 1
    [weight] = state.sql(
        "SELECT model_weight FROM signals WHERE strategy_id = 'bh_flat' AND as_of = ?",
        [DAY2.isoformat()],
    )
    assert weight["model_weight"] == pytest.approx(0.5, abs=0.02)
