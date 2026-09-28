"""CLI tests for intraday TCA (roadmap 21.3.5): `stonks tca intraday` and
`stonks tca calibrate --interval 1m` over a synthetic recording."""

from __future__ import annotations

import json
import math
import tomllib
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest
from typer.testing import CliRunner

from stonks.backtest.costs import CostModelSettings
from stonks.cli import app
from stonks.core.interval import Interval
from stonks.core.stream import QuoteTick
from stonks.store.lake import DuckDBLake
from stonks.store.state import SqliteState
from stonks.streaming.recorder import StreamRecorder

CONFIG = """
[lake]
path = "data/lake.duckdb"

[state]
path = "data/state.sqlite"

[registry]
artifacts_dir = "data/artifacts"

[streaming.record]
dir = "data/streams"
""".strip()

T = "ABC.US"
HALF_SPREAD_BPS = 3.0
IMPACT_BPS = 40.0
DAY = datetime(2026, 9, 21, tzinfo=UTC)


def _at(minute: int, seconds: float = 0.0) -> datetime:
    return DAY + timedelta(hours=14, minutes=30 + minute, seconds=seconds)


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("STONKS_DATA_DIR", raising=False)
    monkeypatch.setenv("COLUMNS", "240")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "default.toml").write_text(CONFIG)
    (tmp_path / "data").mkdir()

    bars, orders = [], []
    with StreamRecorder(tmp_path / "data" / "streams") as rec:
        for i in range(20):
            open_ = 100.0 + 0.1 * i
            volume = 1000 * (i + 1)
            bars.append((_at(i).replace(tzinfo=None), open_, open_ + 0.05, volume))
            half = open_ * HALF_SPREAD_BPS / 10_000
            rec.write(QuoteTick(T, _at(i, 0.5), bid=open_ - half, ask=open_ + half))
            if i == 0:
                continue
            qty = 50.0 * i
            side = "buy" if i % 2 else "sell"
            s = 1.0 if side == "buy" else -1.0
            cost = HALF_SPREAD_BPS + IMPACT_BPS * math.sqrt(qty / volume)
            fill = open_ * (1 + s * cost / 10_000)
            orders.append((f"o{i}", side, qty, _at(i), open_ - 0.05, fill, _at(i, 1)))
        # a wide quote on the next day: after the calibration end
        rec.write(QuoteTick(T, DAY + timedelta(days=1, hours=15), bid=90.0, ask=110.0))

    state = SqliteState(tmp_path / "data" / "state.sqlite")
    state.migrate()
    state.execute(
        "INSERT INTO strategies (id, class_path, params_json, status, created_at, updated_at)"
        " VALUES ('orb', 'x:Y', '{}', 'active', '2026-01-01', '2026-01-01')"
    )
    for cid, side, qty, decided, price, fill, filled_at in orders:
        state.execute(
            "INSERT INTO orders (client_id, strategy_id, ticker, side, quantity, order_type,"
            " status, created_at, updated_at, portfolio_id, decision_price, decided_at,"
            " decision_context_json) VALUES (?, 'orb', ?, ?, ?, 'market', 'filled', ?, ?,"
            " 'pf_default', ?, ?, ?)",
            [cid, T, side, qty, decided.isoformat(), decided.isoformat(), price,
             decided.isoformat(), json.dumps({"trigger": "signal", "interval": "1m"})],
        )  # fmt: skip
        state.execute(
            "INSERT INTO fills (order_client_id, ticker, quantity, price, fee, filled_at,"
            " portfolio_id) VALUES (?, ?, ?, ?, 0.0, ?, 'pf_default')",
            [cid, T, qty, fill, filled_at.isoformat()],
        )
    state.close()

    lake = DuckDBLake(tmp_path / "data" / "lake.duckdb")
    lake.migrate()
    frame = pd.DataFrame(bars, columns=["timestamp", "open", "close", "volume"])
    frame["ticker"] = T
    frame["high"] = frame[["open", "close"]].max(axis=1)
    frame["low"] = frame[["open", "close"]].min(axis=1)
    frame["adj_close"] = frame["close"]
    lake.upsert_bars(frame, Interval.parse("1m"))
    lake.close()
    return tmp_path


def test_intraday_summary_by_sleeve(runner, workdir):
    result = runner.invoke(app, ["tca", "intraday", "--by", "sleeve", "--since", "2026-09-21"])
    assert result.exit_code == 0, result.output
    assert "pf_default/orb" in result.output
    assert "19" in result.output  # orders
    orders = runner.invoke(app, ["tca", "intraday", "--by", "order", "--json"])
    assert orders.exit_code == 0, orders.output
    rows = json.loads(orders.output)
    assert len(rows) == 19
    assert all(r["arrival_source"] == "next_bar" for r in rows)
    assert rows[0]["decision_spread_bps"] == pytest.approx(2 * HALF_SPREAD_BPS, rel=1e-3)
    bad = runner.invoke(app, ["tca", "intraday", "--by", "colour"])
    assert bad.exit_code != 0


def test_calibrate_writes_a_proposal_and_never_applies_it(runner, workdir):
    before = (workdir / "config" / "default.toml").read_text()
    out = workdir / "proposed.toml"
    result = runner.invoke(
        app,
        ["tca", "calibrate", "--interval", "1m", "--end", "2026-09-21", "--out", str(out)],
    )
    assert result.exit_code == 0, result.output
    assert (workdir / "config" / "default.toml").read_text() == before
    parsed = tomllib.loads(out.read_text())
    proposed = CostModelSettings.model_validate(parsed["backtest"]["costs"])
    assert proposed.for_asset_class("equity").half_spread_bps == pytest.approx(
        HALF_SPREAD_BPS, rel=1e-3
    )
    assert proposed.impact_bps == pytest.approx(IMPACT_BPS, rel=1e-2)
    assert "never applied" in out.read_text().lower()


def test_calibrate_prints_the_block_and_refuses_daily(runner, workdir):
    result = runner.invoke(app, ["tca", "calibrate", "--interval", "1m", "--end", "2026-09-21"])
    assert result.exit_code == 0, result.output
    assert "[backtest.costs]" in result.output
    daily = runner.invoke(app, ["tca", "calibrate", "--interval", "1d", "--end", "2026-09-21"])
    assert daily.exit_code != 0
    as_json = runner.invoke(
        app, ["tca", "calibrate", "--interval", "1m", "--end", "2026-09-21", "--json"]
    )
    assert as_json.exit_code == 0, as_json.output
    report = json.loads(as_json.output)
    assert report["fills_used"] == 19
    assert report["quotes"]["equity"] == 20  # the next day's quote is not seen
