"""Cost stress and cost budget (BL-18, after Chan and Carver).

Costs are the least-known input of a backtest, so this test re-runs the
validation backtest with every cost scaled by a multiplier and asks
whether the edge survives:

- Multipliers ``{0, 1, 2, 3}`` by default. A multiplier scales every cost
  field of the dataset's ``CostModelSettings`` (``fee_flat``, ``fee_bps``,
  ``half_spread_bps`` per asset class, ``impact_bps``, and
  ``max_impact_bps`` so the impact cap does not swallow the stress) on a
  copy; the cost model itself is never edited.
- The break-even multiple is the largest multiplier with a positive
  Sharpe, searched up to ``max_break_even`` (grid, then bisection to
  ``bisection_tolerance``).
- The cost Sharpe ``cost_sr = sharpe(0x) - sharpe(1x)`` is the Sharpe the
  costs eat. Carver's speed limit caps it at
  ``min(max_cost_sharpe, max_cost_sharpe_fraction * sharpe(0x))``.

Passes when all hold: ``sharpe(stress) > 0``;
``sharpe(stress) >= min_stressed_sharpe_fraction * sharpe(1x)``; the
break-even multiple is ``>= min_break_even``; and the speed limit holds.
A strategy that makes no trade in the window fails with an "insufficient
data" note (there is nothing to stress).

When the dataset has no costs (``None`` or all zero) the 1x level is
``CostModelSettings.realistic()`` and the report notes it.

The grid backtests run on the lab process pool (``lab.parallel``, via
``_reruns``); the few bisection steps run in-process. Backtests are
deterministic, so the report does not depend on ``max_workers``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from stonks.backtest.costs import AssetClassCosts, CostModelSettings
from stonks.core.protocols import Strategy, SurvivalReport
from stonks.lab.survival._reruns import Rerun, RerunResult, run_reruns
from stonks.logging import get_logger

_log = get_logger("stonks.lab.survival.cost_stress")

_BPS = 10_000.0


class CostStressOptions(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Cost multipliers reported as ``sharpe_<m>x``; must contain 0, 1 and
    #: ``stress_multiplier``.
    multipliers: tuple[float, ...] = (0.0, 1.0, 2.0, 3.0)
    stress_multiplier: float = Field(default=2.0, gt=1.0)
    #: ``sharpe(stress) >= fraction * sharpe(1x)``.
    min_stressed_sharpe_fraction: float = Field(default=0.5, ge=0.0, le=1.0)
    min_break_even: float = Field(default=2.0, ge=0.0)
    #: Upper end of the break-even search.
    max_break_even: float = Field(default=10.0, gt=1.0)
    bisection_tolerance: float = Field(default=0.1, gt=0.0)
    #: Carver's speed limit: ``cost_sr <= min(max_cost_sharpe,
    #: max_cost_sharpe_fraction * sharpe(0x))``.
    max_cost_sharpe: float = Field(default=0.13, ge=0.0)
    max_cost_sharpe_fraction: float = Field(default=1.0 / 3.0, ge=0.0, le=1.0)
    #: Worker processes; ``None`` means ``lab.parallel.default_max_workers()``.
    max_workers: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _levels(self) -> CostStressOptions:
        if any(m < 0 for m in self.multipliers):
            raise ValueError("cost multipliers must be >= 0")
        required = {0.0, 1.0, self.stress_multiplier}
        missing = sorted(required - set(self.multipliers))
        if missing:
            raise ValueError(f"multipliers must include {missing}")
        if self.max_break_even < max(self.multipliers):
            raise ValueError("max_break_even must be >= every multiplier")
        return self


def scale_costs(settings: CostModelSettings, multiplier: float) -> CostModelSettings:
    """A copy of ``settings`` with every cost scaled by ``multiplier``.
    ``max_impact_bps`` scales too, but stays below the bound that keeps
    sell fills at a positive price."""
    m = float(multiplier)

    def scaled(c: AssetClassCosts) -> AssetClassCosts:
        return AssetClassCosts(
            fee_flat=c.fee_flat * m, fee_bps=c.fee_bps * m, half_spread_bps=c.half_spread_bps * m
        )

    default = scaled(settings.default)
    classes = {k: scaled(v) for k, v in settings.asset_classes.items()}
    widest = max(c.half_spread_bps for c in (default, *classes.values()))
    if widest >= _BPS:
        raise ValueError(f"a {m}x half spread reaches 100% of the price")
    headroom = (_BPS - widest) * 0.999
    return CostModelSettings(
        default=default,
        asset_classes=classes,
        impact_bps=settings.impact_bps * m,
        max_impact_bps=min(settings.max_impact_bps * m, headroom),
    )


def _has_costs(settings: CostModelSettings | None) -> bool:
    if settings is None:
        return False
    costs = (settings.default, *settings.asset_classes.values())
    return settings.impact_bps > 0 or any(
        c.fee_flat > 0 or c.fee_bps > 0 or c.half_spread_bps > 0 for c in costs
    )


def break_even_multiple(
    grid: Mapping[float, float],
    sharpe_at: Callable[[float], float],
    *,
    upper: float,
    tolerance: float = 0.1,
) -> float:
    """The largest cost multiple in ``[0, upper]`` with a positive Sharpe:
    0 when even zero costs lose, ``upper`` when ``upper`` still wins,
    otherwise bisected (to ``tolerance``) above the largest known winning
    multiple. ``grid`` holds already known Sharpes; ``sharpe_at``
    computes others."""
    known = dict(grid)

    def sharpe(m: float) -> float:
        if m not in known:
            known[m] = sharpe_at(m)
        return known[m]

    if not sharpe(0.0) > 0:
        return 0.0
    if sharpe(upper) > 0:
        return upper
    winners = [m for m, s in known.items() if m < upper and s > 0]
    lo = max(winners)
    hi = min(m for m in known if m > lo)
    while hi - lo > tolerance:
        mid = (lo + hi) / 2.0
        if sharpe(mid) > 0:
            lo = mid
        else:
            hi = mid
    return lo


def _label(m: float) -> str:
    return f"sharpe_{m:g}x"


class CostStressTest:
    id = "cost_stress"
    Options = CostStressOptions

    def __init__(self, options: CostStressOptions | None = None, **overrides: Any) -> None:
        base = options or CostStressOptions()
        self.options = (
            CostStressOptions.model_validate({**base.model_dump(), **overrides})
            if overrides
            else base
        )

    @classmethod
    def build(cls, options: CostStressOptions) -> CostStressTest:
        return cls(options)

    def run(self, strategy: Strategy, context: Any) -> SurvivalReport:
        opts = self.options
        dataset_costs = getattr(context, "costs", None)
        realistic = not _has_costs(dataset_costs)
        base_costs = CostModelSettings.realistic() if realistic else dataset_costs
        assert base_costs is not None
        levels = sorted({*opts.multipliers, opts.max_break_even})
        _log.info("cost_stress.start", levels=levels, realistic=realistic)

        def reruns(ms: list[float]) -> list[RerunResult]:
            return run_reruns(
                strategy,
                context,
                [Rerun(costs=scale_costs(base_costs, m), override_costs=True) for m in ms],
                max_workers=opts.max_workers if len(ms) > 1 else 1,
                log_prefix="cost_stress",
            )

        results = dict(zip(levels, reruns(levels), strict=True))
        errors = [f"{m:g}x: {r.error}" for m, r in results.items() if not r.ok]
        base = results[1.0]
        costs_note = "no dataset costs, 1x = CostModelSettings.realistic()" if realistic else ""
        if errors or base.n_round_trips == 0:
            reason = (
                "backtest failed: " + "; ".join(errors)
                if errors
                else "insufficient data: no trades in the validation window, nothing to stress"
            )
            return SurvivalReport(
                test_id=self.id,
                passed=False,
                metrics={
                    "n_trades": float(base.n_trades),
                    "used_realistic_costs": float(realistic),
                },
                notes="; ".join(n for n in (reason, costs_note) if n),
            )

        def sharpe_at(m: float) -> float:
            (r,) = reruns([m])
            return r.sharpe if r.ok else float("-inf")

        grid = {m: r.sharpe for m, r in results.items()}
        break_even = break_even_multiple(
            grid, sharpe_at, upper=opts.max_break_even, tolerance=opts.bisection_tolerance
        )
        s0, s1, s_stress = grid[0.0], grid[1.0], grid[opts.stress_multiplier]
        cost_sr = s0 - s1
        cost_sr_limit = min(opts.max_cost_sharpe, opts.max_cost_sharpe_fraction * s0)

        failures = []
        if not s_stress > 0:
            failures.append(f"sharpe at {opts.stress_multiplier:g}x costs {s_stress:.3f} <= 0")
        if s_stress < opts.min_stressed_sharpe_fraction * s1:
            failures.append(
                f"sharpe at {opts.stress_multiplier:g}x costs {s_stress:.3f} < "
                f"{opts.min_stressed_sharpe_fraction:g} x sharpe at 1x ({s1:.3f})"
            )
        if break_even < opts.min_break_even:
            failures.append(f"break-even multiple {break_even:.2f} < {opts.min_break_even:g}")
        if cost_sr > cost_sr_limit:
            failures.append(f"speed limit: cost Sharpe {cost_sr:.3f} > {cost_sr_limit:.3f}")

        metrics = {_label(m): grid[m] for m in opts.multipliers}
        metrics.update(
            {
                "break_even_multiple": float(break_even),
                "cost_sr": float(cost_sr),
                "cost_sr_limit": float(cost_sr_limit),
                "cost_drag_annual": base.cost_drag_annual,
                "turnover_annual": base.turnover_annual,
                "n_trades": float(base.n_trades),
                "used_realistic_costs": float(realistic),
            }
        )
        verdict = "; ".join(failures) if failures else "edge survives the cost stress"
        return SurvivalReport(
            test_id=self.id,
            passed=not failures,
            metrics=metrics,
            notes="; ".join(n for n in (verdict, costs_note) if n),
        )
