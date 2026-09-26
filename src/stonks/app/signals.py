"""SignalService: signal research jobs (BL-33). ``signal_ic`` scores a
strategy's ``estimate_return`` across a universe and window and reports
how well it ranks forward returns (see :mod:`stonks.lab.signal_eval`).

Research only: reads the lake, writes nothing but the job row.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Self

from pydantic import BaseModel, Field, field_validator, model_validator

from stonks.app.context import AppContext
from stonks.app.errors import ValidationError
from stonks.app.jobs import Job, JobContext, JobRunner
from stonks.app.strategies import StrategyRef, StrategyService
from stonks.core.interval import Interval
from stonks.lab.dataset import LabDataset
from stonks.lab.signal_eval import DEFAULT_HORIZONS, signal_ic

SIGNAL_IC_JOB = "signal_ic"

MAX_HORIZON_BARS = 504


class SignalICRequest(BaseModel):
    strategy: StrategyRef
    universe: list[str] = Field(min_length=1, max_length=5000)
    start: date
    end: date
    interval: str = "1d"
    #: Forward-return horizons in bars (the decay curve).
    horizons: list[int] = Field(default=list(DEFAULT_HORIZONS), min_length=1, max_length=20)
    #: Score every this many bars of the window.
    every_bars: int = Field(default=5, ge=1, le=MAX_HORIZON_BARS)
    n_quantiles: int = Field(default=5, ge=2, le=20)
    #: Fewest names with a score and a return for a date's IC.
    min_names: int = Field(default=5, ge=2, le=1000)

    @field_validator("horizons")
    @classmethod
    def _horizons(cls, value: list[int]) -> list[int]:
        if any(h < 1 or h > MAX_HORIZON_BARS for h in value):
            raise ValueError(f"horizons must be 1-{MAX_HORIZON_BARS} bars")
        return sorted(set(value))

    @model_validator(mode="after")
    def _window(self) -> Self:
        if self.start >= self.end:
            raise ValueError("start must be before end")
        return self


class HorizonICView(BaseModel):
    horizon: int
    n_dates: int
    mean_ic: float | None
    ic_std: float | None
    icir: float | None
    #: Share of dates with a positive IC.
    hit_rate: float | None
    se_iid: float | None
    se_hac: float | None
    hac_lags: int
    t_stat_hac: float | None
    #: Mean forward return per score bucket, lowest scores first.
    quantile_means: list[float | None]
    #: Mean top-minus-bottom bucket return, and its HAC t-stat.
    spread_mean: float | None
    spread_t_hac: float | None


class SignalICView(BaseModel):
    """:class:`stonks.lab.signal_eval.SignalICResult`; NaN is null."""

    strategy_id: str
    window: tuple[str, str]
    n_tickers: int
    n_dates: int
    every_bars: int
    n_quantiles: int
    #: ``ok`` or ``n/a`` (see ``note``).
    status: str
    note: str = ""
    horizons: list[HorizonICView] = Field(default_factory=list)
    score_turnover: float | None = None
    top_quantile_turnover: float | None = None
    ic_horizon: int | None = None
    #: Mean IC at ``ic_horizon``: the strategy's IC estimate.
    ic_estimate: float | None = None


def _interval(code: str) -> Interval:
    try:
        return Interval.parse(code)
    except (ValueError, TypeError) as exc:
        raise ValidationError(f"invalid interval {code!r}: {exc}") from None


class SignalService:
    def __init__(self, context: AppContext, strategies: StrategyService, runner: JobRunner) -> None:
        self._ctx = context
        self._strategies = strategies
        self._runner = runner
        runner.register(SIGNAL_IC_JOB, self._handle_signal_ic)

    def submit_signal_ic(self, request: SignalICRequest) -> Job:
        _interval(request.interval)
        self._strategies.resolve(request.strategy)  # validate before queueing
        return self._runner.submit(SIGNAL_IC_JOB, request.model_dump(mode="json"))

    def run_signal_ic(self, request: SignalICRequest) -> SignalICView:
        strategy = self._strategies.resolve(request.strategy)
        parallel = self._ctx.settings.lab.parallel
        with self._ctx.lake() as lake:
            dataset = LabDataset(
                lake=lake,
                universe=list(request.universe),
                start=request.start,
                end=request.end,
                interval=_interval(request.interval),
            )
            result = signal_ic(
                strategy,
                dataset,
                request.horizons,
                request.every_bars,
                n_quantiles=request.n_quantiles,
                min_names=request.min_names,
                max_workers=parallel.max_workers or None,
            )
        return SignalICView.model_validate(result.to_dict())

    def _handle_signal_ic(self, params: dict[str, Any], ctx: JobContext) -> SignalICView:
        return self.run_signal_ic(SignalICRequest.model_validate(params))
