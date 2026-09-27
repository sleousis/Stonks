"""Price alerts (roadmap 20.2): the pure check and one run over the lake."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest

from stonks.accounts import Role, UserRepository
from stonks.notify.router import NotificationRouter
from stonks.price_alerts import AlertRule, Observation, check, run_price_alerts
from stonks.price_alerts.evaluate import firing_event
from stonks.store.lake import DuckDBLake
from stonks.store.state import SqliteState

NOW = datetime(2026, 4, 2, 22, 0, tzinfo=UTC)


def _rule(condition, **kw) -> AlertRule:
    return AlertRule(id="pal_1", owner_id="usr_a", condition=condition, tickers=("UP.US",), **kw)


def _obs(price: float, day: str = "2026-04-01") -> Observation:
    return Observation(ticker="UP.US", observed_at=day, price=price)


# ---- check ----------------------------------------------------------------------------


def test_crosses_above_fires_only_when_the_level_is_passed():
    rule = _rule("crosses_above", level=100.0)
    assert check(rule, _obs(101.0), previous_price=99.0) is not None
    assert check(rule, _obs(100.0), previous_price=99.0) is not None  # touching counts
    assert check(rule, _obs(102.0), previous_price=100.5) is None  # already above
    assert check(rule, _obs(98.0), previous_price=97.0) is None
    assert check(rule, _obs(101.0), previous_price=None) is None  # nothing to compare


def test_crosses_below_fires_on_the_way_down():
    rule = _rule("crosses_below", level=50.0, name="stop")
    fired = check(rule, _obs(49.0), previous_price=51.0)
    assert fired is not None
    assert fired.title == "UP.US crossed below 50" and fired.detail.startswith("stop: ")
    assert check(rule, _obs(51.0), previous_price=49.0) is None


def _history(prices: list[float], start: date = date(2026, 3, 1)) -> list[tuple[date, float]]:
    return [(start + timedelta(days=i), p) for i, p in enumerate(prices)]


def test_moves_pct_fires_when_the_move_becomes_big_enough():
    rule = _rule("moves_pct", pct=10.0, window_days=5)
    flat = _history([100.0] * 10)
    assert check(rule, _obs(100.0), previous_price=None, history=flat) is None
    jump = _history([100.0] * 9 + [111.0])
    fired = check(rule, _obs(111.0), previous_price=None, history=jump)
    assert fired is not None and "moved up 11.0% in 5 days" in fired.title
    down = _history([100.0] * 9 + [85.0])
    assert "moved down 15.0%" in check(rule, _obs(85.0), previous_price=None, history=down).title


def test_moves_pct_does_not_fire_again_while_it_stays_true():
    rule = _rule("moves_pct", pct=10.0, window_days=3)
    history = _history([100.0] * 6 + [112.0, 113.0])
    assert check(rule, _obs(113.0), previous_price=None, history=history) is None


def test_moves_pct_needs_enough_history():
    rule = _rule("moves_pct", pct=1.0, window_days=30)
    assert check(rule, _obs(120.0), previous_price=None, history=_history([100.0, 120.0])) is None


def test_firing_event_goes_to_the_owner_only_with_a_dedupe_key():
    fired = check(_rule("crosses_above", level=100.0), _obs(101.0), previous_price=99.0)
    event = firing_event(fired)
    assert event.audience.user_ids == ("usr_a",)
    assert event.dedupe_key == "price:pal_1:UP.US:2026-04-01"
    assert event.category == "signal" and event.deep_link == "/alerts"


# ---- one run ----------------------------------------------------------------------------


@pytest.fixture
def lake(tmp_path):
    lk = DuckDBLake(tmp_path / "lake.duckdb")
    lk.migrate()
    yield lk
    lk.close()


def _bars(lake, ticker: str, closes: list[tuple[str, float]]) -> None:
    lake.upsert_prices(
        pd.DataFrame(
            [
                {
                    "ticker": ticker,
                    "date": date.fromisoformat(d),
                    "open": c,
                    "high": c,
                    "low": c,
                    "close": c,
                    "adj_close": c,
                    "volume": 100,
                }
                for d, c in closes
            ]
        )
    )


@pytest.fixture
def state(tmp_path):
    st = SqliteState(tmp_path / "state.sqlite")
    st.migrate()
    yield st
    st.close()


def _user(state, name: str) -> str:
    return (
        UserRepository(state)
        .create(display_name=name, role=Role.TRADER, actor="t", email=f"{name}@x.io")
        .id
    )


def _add_rule(state, rid, owner, condition, *, ticker=None, watchlist=None, **kw) -> None:
    state.execute(
        "INSERT INTO price_alert_rules (id, owner_id, target_kind, ticker, watchlist_id,"
        " condition, level, pct, window_days, enabled, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            rid,
            owner,
            "ticker" if ticker else "watchlist",
            ticker,
            watchlist,
            condition,
            kw.get("level"),
            kw.get("pct"),
            kw.get("window_days"),
            int(kw.get("enabled", True)),
            "2026-01-01",
            "2026-01-01",
        ],
    )


def test_a_run_fires_once_publishes_and_remembers(state, lake):
    alice = _user(state, "alice")
    _bars(lake, "UP.US", [("2026-03-30", 98.0), ("2026-03-31", 99.0), ("2026-04-01", 101.0)])
    _add_rule(state, "pal_a", alice, "crosses_above", ticker="UP.US", level=100.0)
    sent = []
    first = run_price_alerts(state, lake, as_of=date(2026, 4, 1), publish=sent.append, now=NOW)
    assert (first.checked, first.fired, first.published) == (1, 1, 1)
    assert sent[0].title == "UP.US crossed above 100"
    again = run_price_alerts(state, lake, as_of=date(2026, 4, 1), publish=sent.append, now=NOW)
    assert (again.checked, again.fired) == (0, 0) and len(sent) == 1
    remembered = state.sql("SELECT * FROM price_alert_state")[0]
    assert remembered["last_price"] == 101.0 and remembered["last_observed_at"] == "2026-04-01"
    # the next day starts from the price it last saw
    _bars(lake, "UP.US", [("2026-04-02", 99.0)])
    down = run_price_alerts(state, lake, as_of=date(2026, 4, 2), publish=sent.append, now=NOW)
    assert down.checked == 1 and down.fired == 0


def test_a_watchlist_rule_checks_each_ticker_of_its_owners_list(state, lake):
    alice, bob = _user(state, "alice"), _user(state, "bob")
    for t in ("UP.US", "DN.US"):
        _bars(lake, t, [("2026-03-31", 100.0), ("2026-04-01", 80.0)])
    state.execute(
        "INSERT INTO watchlists (id, owner_id, name, tickers_json, created_at, updated_at)"
        " VALUES ('wl_a', ?, 'mine', ?, 'x', 'x')",
        [alice, json.dumps(["UP.US", "DN.US", "NOBARS.US"])],
    )
    _add_rule(state, "pal_w", alice, "crosses_below", watchlist="wl_a", level=90.0)
    # Bob points a rule at Alice's list: it sees no ticker.
    _add_rule(state, "pal_b", bob, "crosses_below", watchlist="wl_a", level=90.0)
    _add_rule(state, "pal_off", alice, "crosses_below", ticker="UP.US", level=90.0, enabled=False)
    sent = []
    out = run_price_alerts(state, lake, as_of=date(2026, 4, 1), publish=sent.append, now=NOW)
    assert out.fired == 2 and out.skipped_no_price == 1
    assert {e.audience.user_ids for e in sent} == {(alice,)}
    assert sorted(e.title.split()[0] for e in sent) == ["DN.US", "UP.US"]


def test_the_router_writes_the_owners_feed_and_dedupes(state, lake):
    alice = _user(state, "alice")
    _bars(lake, "UP.US", [("2026-03-31", 99.0), ("2026-04-01", 101.0)])
    _add_rule(state, "pal_a", alice, "crosses_above", ticker="UP.US", level=100.0)
    router = NotificationRouter(state, {})
    run_price_alerts(state, lake, as_of=date(2026, 4, 1), publish=router.publish, now=NOW)
    feed = state.sql("SELECT user_id, category, title FROM alerts")
    assert [(r["user_id"], r["title"]) for r in feed] == [(alice, "UP.US crossed above 100")]
    # the same event again is deduped by the router
    state.execute("DELETE FROM price_alert_state")
    state.execute("DELETE FROM price_alert_events")
    run_price_alerts(state, lake, as_of=date(2026, 4, 1), publish=router.publish, now=NOW)
    assert state.sql("SELECT COUNT(*) AS n FROM alerts")[0]["n"] == 1


def test_a_failing_publish_never_stops_the_run(state, lake):
    alice = _user(state, "alice")
    _bars(lake, "UP.US", [("2026-03-31", 99.0), ("2026-04-01", 101.0)])
    _add_rule(state, "pal_a", alice, "crosses_above", ticker="UP.US", level=100.0)

    def broken(event):
        raise RuntimeError("push service down")

    out = run_price_alerts(state, lake, as_of=date(2026, 4, 1), publish=broken, now=NOW)
    assert out.fired == 1 and out.published == 0
    assert state.sql("SELECT COUNT(*) AS n FROM price_alert_events")[0]["n"] == 1


def test_a_split_between_two_closes_fires_nothing(state, lake):
    # review 2026-09-27: 400 then 100 after a 4:1 split is no move at all
    alice = _user(state, "alice")
    _bars(lake, "UP.US", [("2026-03-30", 400.0), ("2026-03-31", 400.0)])
    _add_rule(state, "pal_down", alice, "crosses_below", ticker="UP.US", level=200.0)
    _add_rule(state, "pal_move", alice, "moves_pct", ticker="UP.US", pct=10.0, window_days=5)
    sent = []
    run_price_alerts(state, lake, as_of=date(2026, 3, 31), publish=sent.append, now=NOW)
    lake.upsert_stock_splits(
        pd.DataFrame([{"ticker": "UP.US", "date": date(2026, 4, 1), "ratio": 4.0}])
    )
    _bars(lake, "UP.US", [("2026-04-01", 100.0)])
    out = run_price_alerts(state, lake, as_of=date(2026, 4, 1), publish=sent.append, now=NOW)
    assert out.fired == 0 and sent == []
