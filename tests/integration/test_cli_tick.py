"""CLI tests for `stonks tick`.

Seeds a fresh lake with canned prices and a registered+active BuyAndHold
strategy, then runs the CLI tick end-to-end.
"""

from __future__ import annotations

import pandas as pd
import pytest
from typer.testing import CliRunner

from stonks.cli import app
from stonks.core.protocols import SurvivalReport
from stonks.ingest.pipeline import _rows_to_df
from stonks.ingest.schemas import TickerProfile
from stonks.registry.store import StrategyRegistry
from stonks.store.lake import DuckDBLake
from stonks.store.state import SqliteState
from stonks.strategies.examples.buy_and_hold import BuyAndHold
from tests.fixtures.governance import seed_status


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "default.toml").write_text(
        """
[lake]
path = "data/lake.duckdb"

[state]
path = "data/state.sqlite"

[registry]
artifacts_dir = "data/artifacts"

[production]
universe = ["UP.US"]
threshold = 0.0
initial_cash = 10000.0

[sources.eodhd]
base_url = "https://example.test/api"
""".strip()
    )
    monkeypatch.setenv("EODHD_API_KEY", "test-key")

    # seed lake + state
    (tmp_path / "data").mkdir()
    lake = DuckDBLake(tmp_path / "data" / "lake.duckdb")
    lake.migrate()
    dates = pd.bdate_range(start="2026-03-01", periods=20)
    closes = [100.0 + i * 2.0 for i in range(len(dates))]
    lake.upsert_prices(
        pd.DataFrame(
            [
                {
                    "ticker": "UP.US",
                    "date": d.date(),
                    "open": c,
                    "high": c + 1,
                    "low": c - 1,
                    "close": c,
                    "adj_close": c,
                    "volume": 1_000_000,
                }
                for d, c in zip(dates, closes, strict=False)
            ]
        )
    )
    # Ranker filters by ``instruments.asset_class``; without a row here
    # UP.US would be skipped as "unknown class" and the tick would no-op.
    lake.upsert_instrument_profile(_rows_to_df([TickerProfile(id="UP.US", asset_class="equity")]))
    lake.close()

    state = SqliteState(tmp_path / "data" / "state.sqlite")
    state.migrate()
    registry = StrategyRegistry(state=state, artifacts_dir=tmp_path / "data" / "artifacts")
    sid = registry.register(
        BuyAndHold({"ticker": "UP.US", "allocation": 1.0}),
        reports=[SurvivalReport(test_id="oos", passed=True, metrics={"sharpe_oos": 1.0})],
    )
    seed_status(registry, sid, "active", default_book=True)
    state.close()

    return tmp_path, sid


def test_tick_dry_run_smoke(runner, seeded):
    _, _ = seeded
    result = runner.invoke(app, ["tick", "--dry-run", "--as-of", "2026-03-20"])
    assert result.exit_code == 0, result.output
    assert "dry-run" in result.output


def test_tick_full_run_smoke(runner, seeded):
    tmp_path, _ = seeded
    result = runner.invoke(app, ["tick", "--as-of", "2026-03-20"])
    assert result.exit_code == 0, result.output

    # verify state tables populated
    state = SqliteState(tmp_path / "data" / "state.sqlite")
    try:
        assert state.count_rows("tick_runs") == 1
        assert state.count_rows("orders") >= 1
        assert state.count_rows("fills") >= 1
        assert state.count_rows("portfolio_snapshots") == 1
    finally:
        state.close()


def test_tick_without_as_of_uses_utc_date(runner, seeded, monkeypatch):
    import stonks.production.tick as tick_mod

    monkeypatch.setattr(tick_mod, "utc_today", lambda: pd.Timestamp("2026-03-20").date())
    result = runner.invoke(app, ["tick", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "tick_2026-03-20_" in result.output


def test_tick_with_tickers_override(runner, seeded):
    result = runner.invoke(
        app, ["tick", "--dry-run", "--as-of", "2026-03-20", "--tickers", "UP.US"]
    )
    assert result.exit_code == 0, result.output


def test_be66_the_cli_tick_runs_the_services_code_path(runner, seeded, monkeypatch):
    import stonks.app.ticks as ticks_mod

    seen = []
    real = ticks_mod.execute_tick

    def spy(settings, state, lake, registry, request):
        seen.append(request)
        return real(settings, state, lake, registry, request)

    monkeypatch.setattr(ticks_mod, "execute_tick", spy)
    result = runner.invoke(
        app, ["tick", "--dry-run", "--as-of", "2026-03-20", "--tickers", "UP.US", "--full"]
    )
    assert result.exit_code == 0, result.output
    [request] = seen
    assert request.tickers == ["UP.US"] and request.is_scoped is False and request.dry_run


def test_tick_asset_class_filter_keeps_matching(runner, seeded):
    """``--asset-class equity`` keeps the equity-only seeded universe."""
    result = runner.invoke(
        app, ["tick", "--dry-run", "--as-of", "2026-03-20", "--asset-class", "equity"]
    )
    assert result.exit_code == 0, result.output
    assert "dry-run" in result.output


def test_tick_asset_class_filter_empty_universe_raises(runner, seeded):
    """``--asset-class crypto`` against an equity-only universe — the
    filter empties the universe and the CLI raises rather than silently
    no-op'ing the tick (see review I2)."""
    result = runner.invoke(
        app, ["tick", "--dry-run", "--as-of", "2026-03-20", "--asset-class", "crypto"]
    )
    assert result.exit_code != 0, result.output
    assert "no instruments in the universe match" in result.output


def test_tick_asset_class_filter_rejects_invalid_class(runner, seeded):
    """Typos like ``--asset-class crpyto`` fail-fast at the validation
    callback rather than silently filtering to an empty universe."""
    result = runner.invoke(
        app, ["tick", "--dry-run", "--as-of", "2026-03-20", "--asset-class", "crpyto"]
    )
    assert result.exit_code != 0, result.output
    assert "must be one of" in result.output


def test_tick_applies_risk_policy_from_config(runner, seeded):
    tmp_path, _ = seeded
    cfg = tmp_path / "config" / "default.toml"
    cfg.write_text(
        cfg.read_text().replace(
            "[sources.eodhd]", "[production.risk]\nmax_open_positions = 0\n\n[sources.eodhd]"
        )
    )
    result = runner.invoke(app, ["tick", "--as-of", "2026-03-20"])
    assert result.exit_code == 0, result.output
    assert "orders=0" in result.output


def _add_shadow_strategy(tmp_path):
    state = SqliteState(tmp_path / "data" / "state.sqlite")
    try:
        registry = StrategyRegistry(state=state, artifacts_dir=tmp_path / "data" / "artifacts")
        sid = registry.register(
            BuyAndHold({"ticker": "UP.US", "allocation": 0.5}),
            reports=[SurvivalReport(test_id="oos", passed=True, metrics={})],
        )
        seed_status(registry, sid, "shadow")
    finally:
        state.close()


def _count(tmp_path, table):
    state = SqliteState(tmp_path / "data" / "state.sqlite")
    try:
        return state.count_rows(table)
    finally:
        state.close()


def test_tick_evaluates_shadow_strategies(runner, seeded):
    tmp_path, _ = seeded
    _add_shadow_strategy(tmp_path)
    result = runner.invoke(app, ["tick", "--as-of", "2026-03-20"])
    assert result.exit_code == 0, result.output
    assert _count(tmp_path, "shadow_portfolio_snapshots") == 1


def test_tick_respects_shadow_disabled_in_config(runner, seeded):
    tmp_path, _ = seeded
    _add_shadow_strategy(tmp_path)
    cfg = tmp_path / "config" / "default.toml"
    cfg.write_text(cfg.read_text().replace("[production]", "[production]\nshadow_enabled = false"))
    result = runner.invoke(app, ["tick", "--as-of", "2026-03-20"])
    assert result.exit_code == 0, result.output
    assert _count(tmp_path, "shadow_portfolio_snapshots") == 0


def test_tick_failure_posts_to_configured_webhook(runner, seeded, monkeypatch):
    import stonks.notify.webhook as webhook_mod
    import stonks.production.tick as tick_mod

    posts: list[dict] = []

    class FakeResponse:
        def raise_for_status(self):
            return None

    class FakeSession:
        def post(self, url, json=None, timeout=None, headers=None):
            posts.append({"url": url, "json": json})
            return FakeResponse()

    monkeypatch.setattr(webhook_mod.requests, "Session", FakeSession)
    monkeypatch.setenv("STONKS_NOTIFY_WEBHOOK_URL", "https://hooks.example.test/tok")

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(tick_mod, "_snapshot_portfolio", boom)
    tmp_path, _ = seeded
    cfg = tmp_path / "config" / "default.toml"
    cfg.write_text(
        cfg.read_text().replace(
            "[sources.eodhd]", '[notify]\nbackends = ["webhook"]\n\n[sources.eodhd]'
        )
    )
    result = runner.invoke(app, ["tick", "--as-of", "2026-03-20"])
    assert result.exit_code != 0
    assert len(posts) == 1
    assert posts[0]["json"]["level"] == "error"


def test_backdated_tick_exits_1_with_clear_error(runner, seeded):
    assert runner.invoke(app, ["tick", "--as-of", "2026-03-20"]).exit_code == 0
    result = runner.invoke(app, ["tick", "--as-of", "2026-03-19"])
    assert result.exit_code == 1
    assert "2026-03-20" in result.output
    assert "Traceback" not in result.output
