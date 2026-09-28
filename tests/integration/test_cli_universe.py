"""CLI tests for `stonks universe ...` and `stonks lab run --universe-id`
(roadmap 10.5). No network: sources are fakes."""

from __future__ import annotations

import json
from datetime import date

import pytest
from typer.testing import CliRunner

from stonks.cli import app
from stonks.ingest.schemas import SymbolListing
from stonks.store.lake import DuckDBLake
from tests.fixtures.universes import FakeListingSource, bars


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def env(tmp_path, monkeypatch, lake_trending):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    lake_path = (tmp_path / "lake.duckdb").as_posix()  # the lake_trending file
    (tmp_path / "config" / "default.toml").write_text(
        f"""
[lake]
path = "{lake_path}"

[state]
path = "data/state.sqlite"

[registry]
artifacts_dir = "data/artifacts"
""".strip()
    )
    (tmp_path / "data").mkdir()
    return tmp_path


@pytest.fixture
def source(monkeypatch) -> FakeListingSource:
    fake = FakeListingSource(
        [
            SymbolListing(ticker="AAA.US", security_type="common_stock"),
            SymbolListing(ticker="DEAD.US", security_type="common_stock", is_delisted=True),
        ],
        prices={
            "AAA.US": bars("AAA.US", date(2025, 6, 2), date(2026, 4, 1)),
            "NEW.US": bars("NEW.US", date(2025, 6, 2), date(2026, 4, 1)),
        },
    )
    import stonks.universes.commands as commands

    monkeypatch.setattr(commands, "build_source", lambda source_id, sources: fake)
    return fake


def _run(runner, *args):
    return runner.invoke(app, list(args), catch_exceptions=False)


def test_create_list_show_members_refresh_delete(runner, env):
    r = _run(runner, "universe", "create", "mine", "--kind", "list", "--tickers", "UP.US,FLAT.US")
    assert r.exit_code == 0, r.output
    assert _run(runner, "universe", "create", "mine", "--tickers", "A.US").exit_code == 1

    listed = _run(runner, "universe", "list")
    assert "mine" in listed.output and "list" in listed.output

    r = _run(runner, "universe", "refresh", "mine", "--as-of", "2026-01-02")
    assert r.exit_code == 0, r.output
    assert "2 members" in r.output

    shown = _run(runner, "universe", "show", "mine")
    assert '"tickers"' in shown.output and "UP.US" in shown.output

    members = _run(runner, "universe", "members", "mine", "--as-of", "2026-01-02")
    assert members.exit_code == 0
    assert members.output.split() == ["FLAT.US", "UP.US"]

    assert _run(runner, "universe", "delete", "mine").exit_code == 1  # needs --yes
    assert _run(runner, "universe", "delete", "mine", "--yes").exit_code == 0
    assert "mine" not in _run(runner, "universe", "list").output


def test_create_from_csv_and_unknown_ids(runner, env):
    csv = env / "u.csv"
    csv.write_text("ticker,start_date,end_date\nUP.US,2020-01-01,\nOLD.US,2010-01-01,2019-01-01\n")
    r = _run(runner, "universe", "create", "fromcsv", "--csv", str(csv))
    assert r.exit_code == 0, r.output
    assert _run(runner, "universe", "show", "nope").exit_code == 1
    assert _run(runner, "universe", "members", "nope").exit_code == 1
    bad = _run(runner, "universe", "create", "r", "--kind", "rule", "--spec", '{"min_adv": 1}')
    assert bad.exit_code != 0


def test_exchange_refresh_and_ensure(runner, env, source):
    spec = json.dumps({"exchange": "US", "security_types": ["common_stock"]})
    r = _run(runner, "universe", "create", "us_all", "--kind", "exchange", "--spec", spec)
    assert r.exit_code == 0, r.output
    r = _run(runner, "universe", "refresh", "us_all")
    assert r.exit_code == 0, r.output
    assert source.calls == ["US"]

    r = _run(runner, "universe", "ensure", "us_all", "--start", "2026-03-02", "--end", "2026-03-31")
    assert r.exit_code == 0, r.output
    assert "fetched" in r.output
    assert {c[0] for c in source.price_calls} == {"AAA.US", "DEAD.US"}


def test_update_and_history(runner, env):
    assert _run(runner, "universe", "create", "mine", "--tickers", "UP.US").exit_code == 0
    r = _run(runner, "universe", "update", "mine", "--tickers", "UP.US,FLAT.US", "--name", "Mine")
    assert r.exit_code == 0, r.output
    shown = _run(runner, "universe", "show", "mine").output
    assert "FLAT.US" in shown and '"Mine"' in shown
    assert _run(runner, "universe", "update", "nope", "--tickers", "A.US").exit_code == 1
    bad = _run(runner, "universe", "update", "mine", "--kind", "rule", "--spec", '{"min_adv": 1}')
    assert bad.exit_code != 0

    spans = json.dumps(
        {"spans": [{"ticker": "OLD.US", "start_date": "2020-01-02", "end_date": "2021-01-04"}]}
    )
    assert _run(runner, "universe", "update", "mine", "--spec", spans).exit_code == 0
    assert _run(runner, "universe", "refresh", "mine", "--as-of", "2026-01-02").exit_code == 0
    history = _run(runner, "universe", "history", "mine")
    assert history.exit_code == 0, history.output
    assert "OLD.US" in history.output and "2020-01-02" in history.output
    assert "2021-01-04" in history.output
    assert _run(runner, "universe", "history", "nope").exit_code == 1


def test_import_index_history(runner, env):
    csv = env / "idx.csv"
    csv.write_text("date,ticker,action\n2024-01-02,AAPL.US,member\n2023-06-01,NEWCO.US,add\n")
    r = _run(runner, "universe", "import-index", "myindex", str(csv))
    assert r.exit_code == 0, r.output
    assert "1 constituents" in r.output and "1 changes" in r.output


def test_lab_run_over_a_universe_id(runner, env):
    assert _run(runner, "universe", "create", "mine", "--tickers", "UP.US,FLAT.US").exit_code == 0
    assert _run(runner, "universe", "refresh", "mine", "--as-of", "2026-01-02").exit_code == 0
    out = env / "result.json"
    r = _run(
        runner,
        "lab",
        "run",
        "momentum",
        "--universe-id",
        "mine",
        "--start",
        "2025-10-01",
        "--end",
        "2026-04-01",
        "--budget",
        "1",
        "--grid-size",
        "1",
        "--json-out",
        str(out),
    )
    assert r.exit_code == 0, r.output
    doc = json.loads(out.read_text())
    assert doc["universe_id"] == "mine"


def test_lab_run_with_ensure_data_fetches_missing_bars(runner, env, source):
    assert _run(runner, "universe", "create", "mine", "--tickers", "UP.US,NEW.US").exit_code == 0
    assert _run(runner, "universe", "refresh", "mine", "--as-of", "2026-01-02").exit_code == 0
    r = _run(
        runner,
        "lab",
        "run",
        "momentum",
        "--universe-id",
        "mine",
        "--ensure-data",
        "--start",
        "2025-10-01",
        "--end",
        "2026-04-01",
        "--budget",
        "1",
        "--grid-size",
        "1",
    )
    assert r.exit_code == 0, r.output
    assert "NEW.US" in {c[0] for c in source.price_calls}
    with DuckDBLake(env / "lake.duckdb") as lake:
        assert not lake.get_prices("NEW.US", date(2025, 10, 1), date(2026, 4, 1)).empty
