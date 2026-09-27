"""Validation of an options strategy (roadmap 17.5, design section 6).

The equity lab's survival suite runs strategies through the stock
backtester, so options strategies get the tests that apply to any equity
curve, run on the options backtest:

- ``oos`` (P7, P8): the strategy is backtested on the last
  ``1 - train_ratio`` of the window only, with a fresh book, and passes
  when its probabilistic Sharpe ratio against zero is at least
  ``min_psr`` with at least ``min_fills`` combo fills;
- ``deflated_sharpe`` (P2, P3): the same Sharpe deflated for
  ``n_trials`` (the trial count the caller has recorded for this class),
  with skill-less trials scattered by the estimator's null variance;
- ``fill_stress``: the full window with fills at ``1.5`` times the half
  spread must still earn a positive Sharpe;
- ``missing_quotes``: dropping ``drop_share`` of the days' chains must
  leave a positive Sharpe;
- ``cost_stress`` (P19): twice the fees must leave a positive Sharpe.

Tests that need re-tuning or a stock universe (MCPT, walk-forward, pool
correlation) do not apply yet; options strategies have few parameters and
are not tuned in the lab (a deviation recorded in the design).
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np

from stonks.backtest.options_engine import (
    OptionMarketData,
    OptionsBacktestConfig,
    OptionsBacktester,
    OptionsBacktestResult,
)
from stonks.core.params import Params
from stonks.core.protocols import SurvivalReport
from stonks.options.strategy import OptionStrategy
from stonks.stats.sharpe import dsr, psr, return_moments, sharpe_variance


@dataclass(frozen=True)
class OptionValidationSettings:
    train_ratio: float = 0.5
    min_psr: float = 0.95
    min_fills: int = 10
    n_trials: int = 1
    stress_spread_fraction: float = 1.5
    drop_share: float = 0.2
    fee_multiple: float = 2.0


def _returns(result: OptionsBacktestResult) -> np.ndarray:
    curve = np.asarray(result.report.equity_curve, dtype=float)
    if curve.size < 2:
        return np.zeros(0)
    return curve[1:] / curve[:-1] - 1.0


def _sharpe(result: OptionsBacktestResult) -> float:
    return return_moments(_returns(result)).sharpe


def validate_option_strategy(
    strategy_cls: type[OptionStrategy],
    params: Params | None,
    data: OptionMarketData,
    config: OptionsBacktestConfig,
    settings: OptionValidationSettings | None = None,
) -> list[SurvivalReport]:
    s = settings or OptionValidationSettings()

    def run(cfg: OptionsBacktestConfig) -> OptionsBacktestResult:
        return OptionsBacktester(strategy_cls(params), data, cfg).run()

    days = sorted(
        {
            d
            for u in config.underlyings
            for d in data.closes.get(u, {})
            if config.start <= d <= config.end
        }
    )
    reports: list[SurvivalReport] = []
    split = days[int(len(days) * s.train_ratio)] if days else config.start
    oos = run(replace(config, start=split))
    m = return_moments(_returns(oos))
    if m.n > 2 and m.sharpe != 0.0:
        p = psr(m.sharpe, 0.0, m.n, m.skew, m.kurt)
        # skill-less trials scatter by the Sharpe estimator's null variance
        var_sr = sharpe_variance(0.0, m.n, m.skew, m.kurt)
        d = dsr(m.sharpe, s.n_trials, var_sr, m.n, m.skew, m.kurt)
    else:
        p = d = 0.0
    reports.append(
        SurvivalReport(
            "oos",
            passed=p >= s.min_psr and oos.n_fills >= s.min_fills,
            metrics={
                "psr": p,
                "sharpe_per_bar": m.sharpe,
                "fills": float(oos.n_fills),
                "bars": float(m.n),
            },
            notes=f"validation window from {split}",
        )
    )
    reports.append(
        SurvivalReport(
            "deflated_sharpe",
            passed=d >= s.min_psr,
            metrics={"dsr": d, "n_trials": float(s.n_trials)},
        )
    )
    base = run(config)
    base_sr = _sharpe(base)
    stressed = run(
        replace(
            config,
            fills=config.fills.model_copy(update={"spread_fraction": s.stress_spread_fraction}),
        )
    )
    reports.append(
        SurvivalReport(
            "fill_stress",
            passed=_sharpe(stressed) > 0,
            metrics={
                "sharpe_per_bar": _sharpe(stressed),
                "base_sharpe_per_bar": base_sr,
                "spread_fraction": s.stress_spread_fraction,
            },
        )
    )
    gaps = run(replace(config, drop_quote_days=s.drop_share))
    reports.append(
        SurvivalReport(
            "missing_quotes",
            passed=_sharpe(gaps) > 0,
            metrics={"sharpe_per_bar": _sharpe(gaps), "drop_share": s.drop_share},
        )
    )
    fees = config.fills
    costly = run(
        replace(
            config,
            fills=fees.model_copy(
                update={
                    "fee_per_contract": fees.fee_per_contract * s.fee_multiple,
                    "fee_per_share": fees.fee_per_share * s.fee_multiple,
                }
            ),
        )
    )
    reports.append(
        SurvivalReport(
            "cost_stress",
            passed=_sharpe(costly) > 0,
            metrics={"sharpe_per_bar": _sharpe(costly), "fee_multiple": s.fee_multiple},
        )
    )
    return reports
