"""LabService — run a backtest or a full lab run (tune → fit → survival
suite → verdict) for a strategy, returning plain data."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from stonks.app.context import AppContext
from stonks.app.errors import ValidationError
from stonks.app.jobs import Job, JobContext, JobRunner
from stonks.app.serialize import finite, to_jsonable
from stonks.app.strategies import StrategyRef, StrategyService, SurvivalReportView
from stonks.backtest.costs import CostModel, CostModelSettings
from stonks.backtest.engine import BacktestConfig, Backtester
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.interval import Interval
from stonks.core.protocols import Objective, Strategy, SurvivalReport, SurvivalTest, Tuner
from stonks.core.types import Portfolio
from stonks.lab.dataset import LabDataset
from stonks.lab.objectives import CAGRObjective, FinalReturnObjective, SharpeObjective
from stonks.lab.runner import LabRunner
from stonks.lab.survival.base import SurvivalSuite
from stonks.lab.survival.drift import DriftTest
from stonks.lab.survival.oos import OutOfSampleTest
from stonks.lab.survival.period_stability import PeriodStabilityTest
from stonks.lab.survival.permutation import MonteCarloPermutationTest
from stonks.lab.survival.perturbation import PerturbationTest
from stonks.lab.survival.runs_test import RunsTestSurvivalTest
from stonks.lab.survival.walk_forward import WalkForwardConfig, WalkForwardTest
from stonks.lab.tuning.grid import GridTuner
from stonks.lab.tuning.random import RandomTuner
from stonks.logging import get_logger

BACKTEST_JOB = "backtest"
LAB_RUN_JOB = "lab_run"

TunerName = Literal["grid", "random"]
ObjectiveName = Literal["sharpe", "cagr", "final_return"]
SurvivalTestName = Literal[
    "oos", "period_stability", "perturbation", "drift", "runs_test", "permutation", "walk_forward"
]
CostModelName = Literal["zero", "realistic"]

_OBJECTIVES: dict[str, Callable[[], Objective]] = {
    "sharpe": SharpeObjective,
    "cagr": CAGRObjective,
    "final_return": FinalReturnObjective,
}
_SURVIVAL_TESTS: dict[str, Callable[[], SurvivalTest]] = {
    "oos": OutOfSampleTest,
    "period_stability": PeriodStabilityTest,
    "perturbation": PerturbationTest,
    "drift": DriftTest,
    "runs_test": RunsTestSurvivalTest,
    "permutation": MonteCarloPermutationTest,
    "walk_forward": WalkForwardTest,
}

_COST_MODELS: dict[str, tuple[str, Callable[[], CostModelSettings]]] = {
    "zero": ("No fees, spread or impact.", CostModelSettings),
    "realistic": (
        "Retail-broker-ish per-asset-class fees and spreads plus square-root market impact.",
        CostModelSettings.realistic,
    ),
}

_log = get_logger("stonks.app.lab")

#: Registers a lab-run result; returns the new strategy id.
RegisterFn = Callable[[Strategy, list[SurvivalReport]], str]


class _WindowRequest(BaseModel):
    strategy: StrategyRef
    universe: list[str] = Field(min_length=1)
    start: date
    end: date
    interval: str = "1d"

    @model_validator(mode="after")
    def _window(self) -> Self:
        if self.start >= self.end:
            raise ValueError("start must be before end")
        return self


class BacktestRequest(_WindowRequest):
    initial_cash: float = Field(default=10_000.0, gt=0)
    threshold: float = 0.0
    rebalance_every_bars: int = Field(default=1, ge=1)
    slippage_bps: float = Field(default=0.0, ge=0)
    fee_per_trade: float = Field(default=0.0, ge=0)
    #: A preset from ``GET /api/lab/cost-models``; replaces the flat
    #: ``slippage_bps`` / ``fee_per_trade`` (set one or the other). With
    #: neither, the configured ``[backtest.costs]`` apply.
    cost_model: CostModelName | None = None

    @model_validator(mode="after")
    def _one_cost_source(self) -> Self:
        if self.cost_model is not None and (self.slippage_bps or self.fee_per_trade):
            raise ValueError("set cost_model or slippage_bps/fee_per_trade, not both")
        return self


class CostModelPreset(BaseModel):
    name: CostModelName
    description: str
    settings: CostModelSettings


class McptOptions(BaseModel):
    """Monte-Carlo permutation test settings (survival test ``permutation``)."""

    model_config = ConfigDict(extra="forbid")

    n_permutations: int = Field(default=50, ge=1, le=1_000)
    max_p_value: float = Field(default=0.05, gt=0, le=1)
    metric: Literal["profit_factor", "sharpe", "final_return", "cagr"] = "profit_factor"
    #: Re-tune on every permutation (Masters); costs ``(n + 1) * budget`` backtests.
    retune: bool = False
    seed: int | None = 17


class EquityPoint(BaseModel):
    timestamp: datetime
    value: float


class BacktestResult(BaseModel):
    strategy_id: str
    interval: str
    start: date
    end: date
    final_return: float | None
    sharpe: float | None
    max_drawdown: float | None
    cagr: float | None
    #: ``None`` when unbounded (gains but no losing bars).
    profit_factor: float | None
    equity: list[EquityPoint]


class LabRunRequest(_WindowRequest):
    """Tunes the class the ``strategy`` ref points at (its ``params`` are
    ignored: the tuner searches the class's parameter space)."""

    train_ratio: float = Field(default=0.7, gt=0, lt=1)
    tuner: TunerName = "random"
    budget: int = Field(default=20, ge=1, le=1_000)
    seed: int = 0
    objective: ObjectiveName = "sharpe"
    survival_tests: list[SurvivalTestName] = Field(
        default_factory=lambda: ["oos", "period_stability"], min_length=1
    )
    #: Settings for the ``walk_forward`` survival test (defaults when omitted).
    walk_forward: WalkForwardConfig | None = None
    #: Settings for the ``permutation`` (MCPT) survival test.
    mcpt: McptOptions | None = None
    #: Register the fitted strategy (status ``shadow``) with its reports.
    register_strategy: bool = False

    @model_validator(mode="after")
    def _options_need_their_test(self) -> Self:
        if self.walk_forward is not None and "walk_forward" not in self.survival_tests:
            raise ValueError("walk_forward options given: add 'walk_forward' to survival_tests")
        if self.mcpt is not None and "permutation" not in self.survival_tests:
            raise ValueError("mcpt options given: add 'permutation' to survival_tests")
        return self


class LabRunView(BaseModel):
    class_path: str
    best_params: dict[str, Any]
    best_score: float | None
    verdict: Literal["pass", "fail"]
    survival_reports: list[SurvivalReportView]
    registered_strategy_id: str | None


class LabService:
    def __init__(self, context: AppContext, strategies: StrategyService, runner: JobRunner) -> None:
        self._ctx = context
        self._strategies = strategies
        self._runner = runner
        runner.register(BACKTEST_JOB, self._handle_backtest)
        # Cooperative: stops between tuning trials and survival tests.
        runner.register(LAB_RUN_JOB, self._handle_lab_run, cancellable=True)

    # ---- backtests ---------------------------------------------------------

    def submit_backtest(self, request: BacktestRequest) -> Job:
        _parse_interval(request.interval)
        self._strategies.resolve(request.strategy)  # validate before queueing
        return self._runner.submit(BACKTEST_JOB, request.model_dump(mode="json"))

    def run_backtest(self, request: BacktestRequest) -> BacktestResult:
        interval = _parse_interval(request.interval)
        strategy = self._strategies.resolve(request.strategy)
        broker = SimulatedBroker(
            portfolio=Portfolio(cash=request.initial_cash, positions={}),
            slippage_bps=request.slippage_bps,
            fee_per_trade=request.fee_per_trade,
            cost_model=self._cost_model(request),
        )
        config = BacktestConfig(
            start=request.start,
            end=request.end,
            universe=list(request.universe),
            interval=interval,
            threshold=request.threshold,
            rebalance_every_bars=request.rebalance_every_bars,
        )
        with self._ctx.lake() as lake:
            report = Backtester(
                strategies=[strategy], broker=broker, lake=lake, config=config
            ).run()
        return BacktestResult(
            strategy_id=report.strategy_id,
            interval=interval.code,
            start=request.start,
            end=request.end,
            final_return=finite(report.final_return),
            sharpe=finite(report.sharpe),
            max_drawdown=finite(report.max_drawdown),
            cagr=finite(report.cagr),
            profit_factor=finite(report.profit_factor),
            equity=[
                EquityPoint(timestamp=_as_datetime(ts), value=float(v))
                for ts, v in zip(report.equity_dates, report.equity_curve, strict=True)
            ],
        )

    def _cost_model(self, request: BacktestRequest) -> CostModel | None:
        """A named preset, else flat slippage / fee when given, else the
        configured ``[backtest.costs]`` (zero costs unless configured)."""
        if request.cost_model is not None:
            return _COST_MODELS[request.cost_model][1]().build()
        if request.slippage_bps or request.fee_per_trade:
            return None
        return self._ctx.settings.backtest.costs.build()

    def cost_models(self) -> list[CostModelPreset]:
        return [
            CostModelPreset(name=name, description=desc, settings=factory())  # type: ignore[arg-type]
            for name, (desc, factory) in _COST_MODELS.items()
        ]

    # ---- lab runs ----------------------------------------------------------

    def submit_lab_run(self, request: LabRunRequest) -> Job:
        _parse_interval(request.interval)
        self._strategies.strategy_class(request.strategy)
        return self._runner.submit(LAB_RUN_JOB, request.model_dump(mode="json"))

    def run_lab(self, request: LabRunRequest, progress: JobContext | None = None) -> LabRunView:
        cls = self._strategies.strategy_class(request.strategy)
        return self.run_lab_class(cls, request, progress=progress)

    def run_lab_class(
        self,
        cls: type[Strategy],
        request: LabRunRequest,
        *,
        progress: JobContext | None = None,
        register: RegisterFn | None = None,
    ) -> LabRunView:
        """The one lab-run code path (API, MCP and Strategy Studio): tunes
        ``cls`` (``request.strategy`` is not resolved here), applies the
        configured ``[backtest.costs]``, defaults walk-forward options from
        ``[lab.walk_forward]`` and, with ``progress``, honours cancellation
        between trials and survival tests. ``register`` replaces the plain
        registry registration (e.g. the studio links the draft too)."""
        interval = _parse_interval(request.interval)
        tuner: Tuner = (
            GridTuner(seed=request.seed) if request.tuner == "grid" else RandomTuner(request.seed)
        )
        objective: Objective = _OBJECTIVES[request.objective]()
        tests: list[SurvivalTest] = [
            _survival_test(t, request, self._ctx.settings.lab.walk_forward)
            for t in request.survival_tests
        ]
        if progress is not None:
            objective = _CancellableObjective(objective, progress)
            tests = [_CancellableTest(t, progress) for t in tests]
        runner = LabRunner(
            tuner=tuner,
            objective=objective,
            suite=SurvivalSuite(tests),
            budget=request.budget,
        )
        if progress is not None:
            progress.progress(0.05, "tuning")
        with self._ctx.lake() as lake:
            dataset = LabDataset(
                lake=lake,
                universe=list(request.universe),
                start=request.start,
                end=request.end,
                train_ratio=request.train_ratio,
                interval=interval,
                costs=self._ctx.settings.backtest.costs,
            )
            result = runner.run(cls, dataset)

        registered: str | None = None
        if request.register_strategy:
            if progress is not None:
                progress.progress(0.95, "registering")
            if register is not None:
                registered = register(result.strategy, list(result.survival_reports))
            else:
                with self._ctx.registry() as registry:
                    registered = registry.register(result.strategy, result.survival_reports)
            _log.info("lab.registered", strategy_id=registered, verdict=result.verdict)

        strategy_cls = type(result.strategy)
        return LabRunView(
            class_path=f"{strategy_cls.__module__}:{strategy_cls.__name__}",
            best_params=to_jsonable(result.best_params),
            best_score=finite(result.best_score),
            verdict=result.verdict,  # type: ignore[arg-type]
            survival_reports=[
                SurvivalReportView(
                    test_id=r.test_id,
                    passed=r.passed,
                    metrics={k: finite(v) for k, v in dict(r.metrics).items()},
                    notes=r.notes,
                )
                for r in result.survival_reports
            ],
            registered_strategy_id=registered,
        )

    # ---- job handlers ------------------------------------------------------

    def _handle_backtest(self, params: dict[str, Any], ctx: JobContext) -> BacktestResult:
        return self.run_backtest(BacktestRequest.model_validate(params))

    def _handle_lab_run(self, params: dict[str, Any], ctx: JobContext) -> LabRunView:
        return self.run_lab(LabRunRequest.model_validate(params), progress=ctx)


class _CancellableObjective:
    """Objective wrapper that honours job cancellation before every trial
    (including the re-tuning trials of walk-forward / MCPT)."""

    def __init__(self, inner: Objective, ctx: JobContext) -> None:
        self._inner = inner
        self._ctx = ctx
        self.name = inner.name
        self.direction = inner.direction

    def score(self, strategy: Any, dataset: Any) -> float:
        self._ctx.check_cancelled()
        return self._inner.score(strategy, dataset)


class _CancellableTest:
    """Survival-test wrapper that honours job cancellation before the test
    starts; forwards everything else (``id``, ``bind_tuning``)."""

    def __init__(self, inner: SurvivalTest, ctx: JobContext) -> None:
        self._inner = inner
        self._ctx = ctx
        self.id = inner.id

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def run(self, strategy: Any, context: Any) -> Any:
        self._ctx.check_cancelled()
        return self._inner.run(strategy, context)


def _survival_test(
    name: str, request: LabRunRequest, walk_forward_default: WalkForwardConfig
) -> SurvivalTest:
    """Request options win; walk-forward otherwise uses ``[lab.walk_forward]``."""
    if name == "walk_forward":
        return WalkForwardTest(config=request.walk_forward or walk_forward_default)
    if name == "permutation" and request.mcpt is not None:
        return MonteCarloPermutationTest(**request.mcpt.model_dump())
    return _SURVIVAL_TESTS[name]()


def _parse_interval(code: str) -> Interval:
    try:
        return Interval.parse(code)
    except (ValueError, TypeError) as exc:
        raise ValidationError(f"invalid interval {code!r}: {exc}") from None


def _as_datetime(value: date | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime(value.year, value.month, value.day)
