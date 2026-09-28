"""Golden single-owner test (design section 3): upgrading an existing
database to the accounts schema changes nothing the tick, P&L or reports
produce.

Two identical installs trade the same days. One stays on the pre-accounts
schema (009); the other is migrated to 010 between its first and second
tick. Every tick result, ledger row (orders, fills, snapshots, shadow books,
tick runs), the P&L series and the rendered HTML report must be identical,
byte for byte.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from stonks.config import GoLivePolicy
from stonks.core.protocols import SurvivalReport
from stonks.production import shadow as shadow_mod
from stonks.production import tick as tick_mod
from stonks.production.pnl import load_pnl
from stonks.production.risk import RiskPolicy
from stonks.production.tick import TickSettings, run_tick
from stonks.registry import artifact as artifact_mod
from stonks.registry import store as registry_mod
from stonks.registry.store import StrategyRegistry
from stonks.reporting.data import build_report
from stonks.reporting.render import render_html
from stonks.store.state import SqliteState
from stonks.strategies.examples.buy_and_hold import BuyAndHold
from stonks.strategies.examples.momentum import Momentum
from tests.fixtures.governance import seed_status
from tests.integration.accounts.legacy import legacy_state, migrate_to_current

FIXED_NOW = "2026-03-21T12:00:00+00:00"
DAYS = [date(2026, 3, 16), date(2026, 3, 18), date(2026, 3, 20), date(2026, 3, 24)]

#: Status changes applied before tick ``i`` (after the upgrade for i >= 1).
STATUS_PLAN = {
    1: [("bh_down", "active")],
    2: [("bh_down", "retired"), ("bh_flat", "active")],
    3: [("bh_flat", "retired")],
}

# The pre-accounts columns of each ledger table: what today's code reads.
LEDGER = {
    "orders": "client_id, tick_id, strategy_id, ticker, side, quantity, order_type,"
    " limit_price, status, status_reason, broker_order_id, created_at, updated_at",
    "fills": "id, order_client_id, ticker, quantity, price, fee, filled_at",
    "portfolio_snapshots": "id, tick_id, as_of, taken_at, cash, positions_json, total_value",
    "shadow_decisions": "id, tick_id, strategy_id, as_of, ticker, side, quantity, price, status,"
    " created_at",
    "shadow_portfolio_snapshots": "*",
    "tick_runs": "*",
}


class _TickIds:
    """Deterministic tick ids (``tick_<as_of>_<n>``), restarted per install."""

    def __init__(self) -> None:
        self.n = 0

    def __call__(self, as_of: date) -> str:
        self.n += 1
        return f"tick_{as_of.isoformat()}_{self.n}"


@pytest.fixture
def tick_ids(monkeypatch) -> _TickIds:
    """Pin wall clocks and random tick ids so two installs can be compared."""
    ids = _TickIds()
    monkeypatch.setattr(tick_mod, "_new_tick_id", ids)
    for mod in (tick_mod, shadow_mod, registry_mod, artifact_mod):
        monkeypatch.setattr(mod, "_iso_now", lambda: FIXED_NOW)
    return ids


def _install(root: Path, monkeypatch) -> tuple[SqliteState, StrategyRegistry]:
    state = legacy_state(root / "state.sqlite", monkeypatch)
    registry = StrategyRegistry(state=state, artifacts_dir=root / "artifacts")
    reports = [SurvivalReport(test_id="oos", passed=True, metrics={"sharpe_oos": 1.0})]
    for sid, strategy, status in [
        ("mom", Momentum({"lookback_days": 5, "threshold": 0.0, "allocation": 0.3}), "active"),
        ("bh_down", BuyAndHold({"ticker": "DOWN.US", "allocation": 0.3}), "shadow"),
        ("bh_flat", BuyAndHold({"ticker": "FLAT.US", "allocation": 0.5}), "shadow"),
    ]:
        registry.register(strategy, reports=reports, strategy_id=sid)
        if status != "shadow":
            seed_status(registry, sid, status)
    return state, registry


def _snapshot(state: SqliteState, registry: StrategyRegistry) -> dict:
    out: dict = {}
    for table, cols in LEDGER.items():
        rows = state.sql(f"SELECT {cols} FROM {table} ORDER BY rowid")
        # Compare the legacy columns only; the migration adds new ones.
        legacy = [c.strip() for c in cols.split(",")] if cols != "*" else None
        out[table] = [
            tuple(r[c] for c in legacy) if legacy else tuple(dict(r).items()) for r in rows
        ]
    out["pnl"] = [dataclasses.astuple(r) for r in load_pnl(state)]
    report = build_report(
        state, registry, GoLivePolicy(), now=datetime.fromisoformat(FIXED_NOW).astimezone(UTC)
    )
    out["report_html"] = render_html(report)
    return out


def _scenario(root: Path, lake, monkeypatch, ids: _TickIds, *, upgrade: bool) -> tuple[list, dict]:
    ids.n = 0
    state, registry = _install(root, monkeypatch)
    settings = TickSettings(
        universe=["UP.US", "FLAT.US", "DOWN.US"],
        threshold=0.0,
        initial_cash=10_000.0,
        slippage_bps=5.0,
        fee_per_trade=1.0,
        risk=RiskPolicy(max_weight_per_ticker=0.5, cash_buffer_fraction=0.02),
    )
    results = []
    for i, day in enumerate(DAYS):
        if upgrade and i == 1:
            migrate_to_current(state)
        # Governance moves between ticks, so every tick after the upgrade trades.
        for sid, status in STATUS_PLAN.get(i, []):
            seed_status(registry, sid, status)
        result = run_tick(state, lake, registry, settings, as_of=day)
        results.append(dataclasses.asdict(result))
    # A same-day re-run is a no-op either way.
    results.append(dataclasses.asdict(run_tick(state, lake, registry, settings, as_of=DAYS[-1])))
    snap = _snapshot(state, registry)
    if upgrade:
        for table in ("orders", "fills", "portfolio_snapshots"):
            owners = {r[0] for r in state.sql(f"SELECT portfolio_id FROM {table}")}
            assert owners == {"pf_default"}, table
    state.close()
    return results, snap


def test_upgrade_to_accounts_changes_nothing(tmp_path, lake_trending, monkeypatch, tick_ids):
    legacy_results, legacy = _scenario(
        tmp_path / "legacy", lake_trending, monkeypatch, tick_ids, upgrade=False
    )
    upgraded_results, upgraded = _scenario(
        tmp_path / "upgraded", lake_trending, monkeypatch, tick_ids, upgrade=True
    )

    # The scenario must actually trade after the upgrade, or the comparison
    # proves nothing.
    assert all(r["orders_placed"] > 0 for r in legacy_results[: len(DAYS)])
    assert legacy["orders"] and legacy["fills"] and legacy["shadow_decisions"]
    assert len(legacy["pnl"]) == len(DAYS)

    assert upgraded_results == legacy_results
    for key in legacy:
        assert upgraded[key] == legacy[key], key
    assert json.dumps(upgraded, sort_keys=True, default=str) == json.dumps(
        legacy, sort_keys=True, default=str
    )
