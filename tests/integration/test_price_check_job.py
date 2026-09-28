"""The ``price_check`` scheduler action (roadmap 23.6): it skips while off
on every backend, and the local backend compares held and signalled tickers
with the second source, stores the check and alerts on a gap."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest

from stonks.config import Settings
from stonks.ingest.schemas import RawPriceBar
from stonks.production.price_check import latest_check, price_holds
from stonks.scheduling.api_backend import API_ACTIONS
from stonks.scheduling.in_process import IN_PROCESS_ACTIONS
from stonks.scheduling.jobs import JobSpec, RunContext
from stonks.scheduling.local import LOCAL_ACTIONS
from stonks.scheduling.triggers import Fire, SessionTrigger
from stonks.store.lake import DuckDBLake
from stonks.store.state import SqliteState

SESSION = date(2026, 3, 18)
AT = datetime(2026, 3, 18, 20, 42, tzinfo=UTC)
DAYS = pd.bdate_range("2026-02-02", SESSION)


class Notes:
    def __init__(self):
        self.sent = []

    def notify(self, notification):
        self.sent.append(notification)


def _settings(tmp_path, enabled=True) -> Settings:
    s = Settings(
        state={"path": tmp_path / "state.sqlite"},
        lake={"path": tmp_path / "lake.duckdb"},
        notify={"backends": []},
        production={"price_check": {"enabled": enabled}},
    )
    with SqliteState(s.state.path) as state:
        state.migrate()
    lake = DuckDBLake(s.lake.path)
    lake.migrate()
    lake.upsert_prices(
        pd.DataFrame(
            [
                {"ticker": t, "date": d.date(), "open": 1.0, "high": 1.0, "low": 1.0,
                 "close": 50.0 + i, "adj_close": 50.0 + i, "volume": 1}
                for t in ("H.US", "S.US")
                for i, d in enumerate(DAYS)
            ]
        )
    )  # fmt: skip
    lake.close()
    return s


def _ctx(settings, notifier=None) -> RunContext:
    return RunContext(
        spec=JobSpec("price_check", "price_check", SessionTrigger("XNYS", "close", timedelta(0))),
        fire=Fire(AT, SESSION, "k"),
        run_id="srun_t",
        now=AT,
        settings=settings,
        notifier=notifier,  # type: ignore[arg-type]
    )


class Yahoo:
    source_id = "yahoo"

    def __init__(self, off: dict[str, float]):
        self.off = off

    def fetch_prices(self, ticker, since=None, until=None):
        return [
            RawPriceBar(ticker=ticker, date=d.date(), open=1.0, high=1.0, low=1.0,
                        close=(50.0 + i) * self.off.get(ticker, 1.0),
                        adj_close=(50.0 + i) * self.off.get(ticker, 1.0), volume=1)
            for i, d in enumerate(DAYS)
        ]  # fmt: skip


@pytest.mark.parametrize("registry", [LOCAL_ACTIONS, API_ACTIONS, IN_PROCESS_ACTIONS])
def test_the_job_skips_while_off(tmp_path, registry):
    out = registry.get("price_check")(_ctx(_settings(tmp_path, enabled=False)))
    assert out.status == "skipped" and out.detail["reason"] == "disabled"


def test_the_local_job_checks_held_and_signalled_tickers(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    with SqliteState(settings.state.path) as state:
        empty = LOCAL_ACTIONS.get("price_check")(_ctx(settings))
        assert empty.status == "skipped" and empty.detail["reason"] == "no_tickers"
        state.execute("INSERT INTO tick_runs (id, started_at, status) VALUES ('t1', 'x', 'ok')")
        state.execute(
            "INSERT INTO portfolio_snapshots (tick_id, taken_at, cash, positions_json,"
            " total_value, as_of, portfolio_id) VALUES ('t1', 'x', 0, ?, 0, ?, 'pf_default')",
            [json.dumps({"H.US": 2}), "2026-03-17"],
        )
        state.execute(
            "INSERT INTO signals (as_of, strategy_id, ticker, tick_id, score)"
            " VALUES ('2026-03-17', 's1', 'S.US', 't1', 0.1)"
        )
    monkeypatch.setattr(
        "stonks.ingest.sources.registry.build_source", lambda sid, cfg: Yahoo({"S.US": 1.2})
    )
    notes = Notes()
    out = LOCAL_ACTIONS.get("price_check")(_ctx(settings, notes))
    assert out.status == "succeeded" and out.alerted
    assert out.detail == {"status": "gaps", "compared": 2, "held": ["S.US"]}
    assert notes.sent and "S.US" in notes.sent[0].message
    with SqliteState(settings.state.path) as state:
        assert price_holds(state, SESSION) == frozenset({"S.US"})
        row = latest_check(state)
        assert row is not None and row["source"] == "yahoo"
