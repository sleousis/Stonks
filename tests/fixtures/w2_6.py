"""Seed helpers for the incubation-grade go-live gate (BL-25, W2.6)."""

from __future__ import annotations

from typing import Any

from stonks.core.protocols import SurvivalReport
from stonks.lab.survival.registry import resolve_preset
from stonks.registry.artifact import update_meta
from stonks.registry.store import StrategyRegistry

REALISTIC_COSTS: dict[str, Any] = {
    "default": {"fee_flat": 0.0, "fee_bps": 1.0, "half_spread_bps": 2.0},
    "asset_classes": {},
    "impact_bps": 5.0,
    "max_impact_bps": 500.0,
}
ZERO_COSTS: dict[str, Any] = {
    "default": {"fee_flat": 0.0, "fee_bps": 0.0, "half_spread_bps": 0.0},
    "asset_classes": {},
    "impact_bps": 0.0,
    "max_impact_bps": 500.0,
}
HYPOTHESIS = "Momentum persists because investors under-react to news; fails in reversals."


def oos_report(
    sharpe: float = 4.0,
    cagr: float = 0.0,
    max_dd: float = -0.10,
    n_trades: int | None = 50,
    **extra: Any,
) -> SurvivalReport:
    metrics: dict[str, Any] = {
        "sharpe_oos": sharpe,
        "cagr_oos": cagr,
        "max_drawdown_oos": max_dd,
        **extra,
    }
    if n_trades is not None:
        metrics["n_trades"] = n_trades
    return SurvivalReport(test_id="oos", passed=True, metrics=metrics)


def mc_report(p95_max_dd: float = 0.20, **extra: Any) -> SurvivalReport:
    return SurvivalReport(
        test_id="mc_trades",
        passed=True,
        metrics={"p95_max_dd": p95_max_dd, "median_max_dd": p95_max_dd / 2, **extra},
    )


def promotion_reports(oos: SurvivalReport | None = None, mc: SurvivalReport | None = None):
    """A passing report for every registered ``promotion`` preset test,
    plus ``mc_trades`` (the Monte Carlo band) even before it lands."""
    reports = {"oos": oos or oos_report(), "mc_trades": mc or mc_report()}
    for test_id in resolve_preset("promotion"):
        reports.setdefault(test_id, SurvivalReport(test_id=test_id, passed=True, metrics={}))
    return list(reports.values())


def set_meta(
    registry: StrategyRegistry,
    strategy_id: str,
    *,
    hypothesis: str | None = HYPOTHESIS,
    costs: dict[str, Any] | None = None,
    **extra: Any,
) -> None:
    """Write lab provenance (``hypothesis``, ``manifest.costs``) into the
    strategy's ``meta.json``, as ``stonks lab run`` does."""
    handle = next(h for h in registry.list_all() if h.id == strategy_id)
    meta: dict[str, Any] = {"manifest": {"costs": REALISTIC_COSTS if costs is None else costs}}
    if hypothesis is not None:
        meta["hypothesis"] = hypothesis
    meta.update(extra)
    update_meta(handle.artifact_path, meta)
