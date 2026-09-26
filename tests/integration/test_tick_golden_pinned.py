"""Pinned golden tick (BL-12, W2.1): under default settings (one owner, one
portfolio, the ``single_winner`` constructor) the tick writes exactly what
it wrote before the construction pipeline existed.

``tests/fixtures/golden/tick_single_owner.json`` was recorded with the
pre-pipeline tick (``feat/roadmap`` at ``ad36b64``). Wall clocks and tick ids
are pinned, so every ledger row, the tick summaries and the results must
match byte for byte. Regenerate only for an intended behaviour change:
``REGEN_GOLDEN=1 uv run pytest tests/integration/test_tick_golden_pinned.py``.
"""

from __future__ import annotations

import dataclasses
import json
import os
from datetime import date
from pathlib import Path

import pytest

from stonks.core.protocols import SurvivalReport
from stonks.production import shadow as shadow_mod
from stonks.production import tick as tick_mod
from stonks.production.risk import RiskPolicy
from stonks.production.tick import TickSettings, run_tick
from stonks.registry import artifact as artifact_mod
from stonks.registry import store as registry_mod
from stonks.registry.store import StrategyRegistry
from stonks.store.state import SqliteState
from stonks.strategies.examples.buy_and_hold import BuyAndHold
from stonks.strategies.examples.momentum import Momentum
from tests.fixtures.governance import seed_status

GOLDEN = Path(__file__).parents[1] / "fixtures" / "golden" / "tick_single_owner.json"
FIXED_NOW = "2026-03-21T12:00:00+00:00"
#: ``(as_of, ranking threshold)`` per tick. Day 3's threshold ranks
#: nothing, so the owner of the holdings exits them without picks.
DAYS = [
    (date(2026, 3, 16), 0.0),
    (date(2026, 3, 17), 0.0),
    (date(2026, 3, 18), 2.0),
    (date(2026, 3, 20), 0.0),
    (date(2026, 3, 24), 0.0),
]

#: Status changes applied before tick ``i``: a second strategy takes over,
#: retires (its holding falls to the older owner), and finally nobody
#: active owns the book.
STATUS_PLAN = {
    1: [("bh_flat", "active")],
    2: [("bh_flat", "retired")],
    4: [("mom", "retired")],
}

LEDGER = {
    "orders": "client_id, tick_id, strategy_id, ticker, side, quantity, order_type,"
    " limit_price, status, status_reason, broker_order_id, created_at, updated_at",
    "fills": "id, order_client_id, ticker, quantity, price, fee, filled_at",
    "portfolio_snapshots": "id, tick_id, as_of, taken_at, cash, positions_json, total_value",
    "shadow_decisions": "id, tick_id, strategy_id, as_of, ticker, side, quantity, price,"
    " status, created_at",
    "shadow_portfolio_snapshots": "id, tick_id, strategy_id, as_of, taken_at, cash,"
    " positions_json, total_value",
    "tick_runs": "id, started_at, finished_at, status, summary_json",
}


@pytest.fixture
def pinned_clock(monkeypatch):
    counter = {"n": 0}

    def tick_id(as_of: date) -> str:
        counter["n"] += 1
        return f"tick_{as_of.isoformat()}_{counter['n']}"

    monkeypatch.setattr(tick_mod, "_new_tick_id", tick_id)
    for mod in (tick_mod, shadow_mod, registry_mod, artifact_mod):
        monkeypatch.setattr(mod, "_iso_now", lambda: FIXED_NOW)


def _run(tmp_path: Path, lake) -> dict:
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    registry = StrategyRegistry(state=state, artifacts_dir=tmp_path / "artifacts")
    reports = [SurvivalReport(test_id="oos", passed=True, metrics={"sharpe_oos": 1.0})]
    for sid, strategy, status in [
        ("mom", Momentum({"lookback_days": 5, "threshold": 0.0, "allocation": 0.6}), "active"),
        ("bh_flat", BuyAndHold({"ticker": "FLAT.US", "allocation": 0.5}), "shadow"),
        ("bh_down", BuyAndHold({"ticker": "DOWN.US", "allocation": 0.4}), "shadow"),
    ]:
        registry.register(strategy, reports=reports, strategy_id=sid)
        if status != "shadow":
            seed_status(registry, sid, status)
    def settings(threshold: float) -> TickSettings:
        return TickSettings(
            universe=["UP.US", "FLAT.US", "DOWN.US"],
            threshold=threshold,
            initial_cash=10_000.0,
            slippage_bps=5.0,
            fee_per_trade=1.0,
            risk=RiskPolicy(max_weight_per_ticker=0.5, cash_buffer_fraction=0.02),
        )

    results: list[dict] = []

    def tick(i: int, **kw) -> None:
        day, threshold = DAYS[i]
        result = run_tick(state, lake, registry, settings(threshold), as_of=day, **kw)
        results.append(dataclasses.asdict(result))

    tick(0, dry_run=True)  # writes nothing but its tick_runs row
    for i in range(len(DAYS)):
        for sid, status in STATUS_PLAN.get(i, []):
            seed_status(registry, sid, status)
        tick(i)
        if i == 3:
            tick(i)  # a same-day re-run places nothing twice
    out: dict = {"results": [_legacy_result(r) for r in results]}
    for table, cols in LEDGER.items():
        rows = state.sql(f"SELECT {cols} FROM {table} ORDER BY rowid")
        out[table] = [list(tuple(r)) for r in rows]
    state.close()
    return out


def _legacy_result(result: dict) -> dict:
    """The fields ``TickResult`` had when the golden file was recorded."""
    keys = ("tick_id", "status", "winner_strategy_id", "orders_placed", "fills")
    return {k: result[k] for k in keys}


def test_default_tick_matches_the_pinned_golden(tmp_path, lake_trending, pinned_clock):
    got = json.loads(json.dumps(_run(tmp_path, lake_trending), sort_keys=True))
    if os.environ.get("REGEN_GOLDEN") == "1":
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(got, sort_keys=True, indent=1) + "\n", encoding="utf-8")
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    # The scenario must trade, exit and run shadow books, or it proves little.
    assert any(r["orders_placed"] for r in expected["results"])
    assert expected["shadow_decisions"] and expected["fills"]
    for key in expected:
        assert got[key] == expected[key], key
    assert got == expected
