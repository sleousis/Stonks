"""``python -m stonks.streaming`` (roadmap 21.1)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from stonks.config import Settings
from stonks.core.interval import Interval
from stonks.core.stream import Heartbeat, TradeTick
from stonks.store.lake import DuckDBLake
from stonks.streaming import __main__ as cli
from stonks.streaming.recorder import StreamRecorder

M0 = datetime(2026, 9, 28, 13, 30, tzinfo=UTC)


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Settings:
    s = Settings()
    s.lake.path = tmp_path / "lake.duckdb"
    s.state.path = tmp_path / "state.sqlite"
    with DuckDBLake(s.lake.path) as lake:
        lake.migrate()
    monkeypatch.setattr(cli, "_settings", lambda: s)
    return s


def health_of(out: str) -> dict:
    """The health JSON the command prints last (log lines come before it)."""
    return json.loads(out[out.index('{\n  "source"') :])


def recording(tmp_path):
    root = tmp_path / "rec"
    with StreamRecorder(root) as rec:
        for sec, price in ((1, 10.0), (30, 11.0), (61, 12.0)):
            rec.write(TradeTick("A.US", M0 + timedelta(seconds=sec), price, 2))
        rec.write(Heartbeat(M0 + timedelta(seconds=200)))
    return root


def test_sources_lists_every_registered_source(capsys):
    assert cli.main(["sources"]) == 0
    out = capsys.readouterr().out
    assert "eodhd\tlive" in out and "ibkr\tlive" in out and "replay\tfinite" in out


def test_run_refuses_while_streaming_is_off(settings, capsys):
    assert settings.streaming.enabled is False
    assert cli.main(["run", "--tickers", "AAPL.US"]) == 2
    assert "enabled" in capsys.readouterr().err


def test_run_needs_tickers(settings, capsys):
    settings.streaming.enabled = True
    assert cli.main(["run"]) == 2
    assert "tickers" in capsys.readouterr().err


def test_replay_prints_health(settings, tmp_path, capsys):
    root = recording(tmp_path)
    assert cli.main(["replay", str(root)]) == 0
    health = health_of(capsys.readouterr().out)
    assert health["events"]["trade"] == 3 and health["state"] == "stopped"


def test_replay_write_stores_bars(settings, tmp_path, capsys):
    root = recording(tmp_path)
    assert cli.main(["replay", str(root), "--write"]) == 0
    with DuckDBLake(settings.lake.path) as lake:
        got = lake.get_bars("A.US", Interval.MIN_1, datetime(2026, 1, 1), datetime(2027, 1, 1))
    assert list(got["close"]) == [11.0, 12.0]
    assert health_of(capsys.readouterr().out)["bars_written"] == 2


def test_run_streams_a_replay_into_the_lake(settings, tmp_path, capsys):
    root = recording(tmp_path)
    settings.streaming.enabled = True
    settings.streaming.source = "replay"
    settings.streaming.replay.path = str(root)
    settings.streaming.backfill = False
    assert cli.main(["run", "--tickers", "A.US"]) == 0
    assert health_of(capsys.readouterr().out)["bars_written"] == 2


def test_record_saves_parquet_without_the_lake(settings, tmp_path, capsys):
    root = recording(tmp_path)
    settings.streaming.source = "replay"
    settings.streaming.replay.path = str(root)
    out_dir = tmp_path / "copy"
    assert cli.main(["record", "--tickers", "A.US", "--dir", str(out_dir)]) == 0
    assert any(out_dir.rglob("*.parquet"))
    assert health_of(capsys.readouterr().out)["bars_written"] == 0
