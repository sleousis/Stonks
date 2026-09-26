"""LabService — run a backtest or a full lab run (tune → fit → survival
suite → verdict) for a strategy, returning plain data.

:func:`execute_lab_run` is the one lab-run code path: the API, MCP and the
Strategy Studio reach it through :class:`LabService`, the CLI's
``stonks lab run`` and ``stonks lab sweep`` call it directly. It builds the
tuner (``[lab.parallel]`` workers), the suite (survival-test registry), the
dataset (cost model), pre-registers the run in the trial ledger, and
registers the result (with its lab provenance in ``meta.json``) when asked.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Annotated, Any, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, WithJsonSchema, model_validator

from stonks.app.context import AppContext
from stonks.app.errors import ValidationError
from stonks.app.jobs import Job, JobContext, JobRunner
from stonks.app.serialize import finite, to_jsonable
from stonks.app.strategies import StrategyRef, StrategyService, SurvivalReportView
from stonks.backtest import metrics as bt_metrics
from stonks.backtest.costs import CostModel, CostModelSettings
from stonks.backtest.engine import BacktestConfig, Backtester
from stonks.backtest.report import BacktestReport
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.backtest.trades import with_trades
from stonks.core.interval import Interval
from stonks.core.protocols import Objective, Strategy, SurvivalReport, SurvivalTest, Tuner
from stonks.core.types import Portfolio
from stonks.lab.dataset import LabDataset
from stonks.lab.objectives import CAGRObjective, FinalReturnObjective, SharpeObjective
from stonks.lab.parallel import ParallelSettings
from stonks.lab.runner import LabRunner, LabRunResult, costs_are_zero
from stonks.lab.survival.base import SurvivalSuite
from stonks.lab.survival.registry import (
    build_survival_test,
    preset_names,
    resolve_suite,
    survival_test_names,
)
from stonks.lab.survival.walk_forward import WalkForwardConfig
from stonks.lab.trials import TrialLedger
from stonks.lab.tuning.grid import GridTuner
from stonks.lab.tuning.random import RandomTuner
from stonks.logging import get_logger
from stonks.registry.artifact import update_meta
from stonks.registry.store import StrategyRegistry

BACKTEST_JOB = "backtest"
LAB_RUN_JOB = "lab_run"

TunerName = Literal["grid", "random"]
ObjectiveName = Literal["sharpe", "cagr", "final_return"]
CostModelName = Literal["zero", "realistic"]

#: API names kept from before the survival-test registry (BL-10).
_LEGACY_TEST_NAMES: dict[str, str] = {"permutation": "mcpt"}


def _accepted_test_names() -> list[str]:
    return sorted({*survival_test_names(), *_LEGACY_TEST_NAMES})


def _check_test_name(name: str) -> str:
    if name not in _LEGACY_TEST_NAMES and name not in survival_test_names():
        raise ValueError(f"unknown survival test {name!r}; choose from {_accepted_test_names()}")
    return name


def _check_preset(name: str) -> str:
    if name not in preset_names():
        raise ValueError(f"unknown survival preset {name!r}; choose from {preset_names()}")
    return name


#: A survival test id from ``stonks.lab.survival.registry`` (plus the legacy
#: ``permutation`` alias of ``mcpt``), validated against the registry.
SurvivalTestName = Annotated[
    str,
    AfterValidator(_check_test_name),
    WithJsonSchema({"type": "string", "enum": _accepted_test_names()}),
]
#: A named suite from ``SUITE_PRESETS`` (``quick``, ``standard``, ``promotion``).
SurvivalPresetName = Annotated[
    str,
    AfterValidator(_check_preset),
    WithJsonSchema({"type": "string", "enum": preset_names()}),
]
#: A cost preset name (``GET /api/lab/cost-models``) or explicit settings.
CostModelOption = CostModelName | CostModelSettings

_OBJECTIVES: dict[str, Callable[[], Objective]] = {
    "sharpe": SharpeObjective,
    "cagr": CAGRObjective,
    "final_return": FinalReturnObjective,
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


class BacktestOptions(BaseModel):
    """Backtest settings shared by :class:`BacktestRequest` and the Studio's
    draft backtests."""

    initial_cash: float = Field(default=10_000.0, gt=0)
    threshold: float = 0.0
    rebalance_every_bars: int = Field(default=1, ge=1)
    slippage_bps: float = Field(default=0.0, ge=0)
    fee_per_trade: float = Field(default=0.0, ge=0)
    #: A preset from ``GET /api/lab/cost-models`` or explicit cost-model
    #: settings; replaces the flat ``slippage_bps`` / ``fee_per_trade`` (set
    #: one or the other). With neither, the configured ``[backtest.costs]`` apply.
    cost_model: CostModelOption | None = None

    @model_validator(mode="after")
    def _one_cost_source(self) -> Self:
        if self.cost_model is not None and (self.slippage_bps or self.fee_per_trade):
            raise ValueError("set cost_model or slippage_bps/fee_per_trade, not both")
        return self


class BacktestRequest(_WindowRequest, BacktestOptions):
    pass


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


class TradeStatsView(BaseModel):
    """Trade-level statistics of a backtest (``backtest.trades.TradeStats``).
    Win/loss figures are over closed round trips; ``None`` marks an
    unbounded ratio (no losing trades)."""

    n_trades: int
    n_open: int
    win_rate: float
    avg_win: float
    avg_loss: float
    payoff_ratio: float | None
    #: Mean P&L per closed trade, in cash.
    expectancy: float
    trade_profit_factor: float | None
    avg_bars_held: float
    #: Share of bars that closed with a position held.
    exposure: float
    turnover_annual: float
    costs_paid: float
    cost_drag_annual: float


class TradeView(BaseModel):
    """One round trip (a lot, or part of one, from buy to sell or to the end)."""

    ticker: str
    entry_ts: datetime
    exit_ts: datetime
    qty: float
    entry_px: float
    exit_px: float
    pnl: float
    return_pct: float
    bars_held: int
    fees: float
    is_open: bool


class BacktestResult(BaseModel):
    strategy_id: str
    interval: str
    start: date
    end: date
    final_return: float | None
    sharpe: float | None
    max_drawdown: float | None
    cagr: float | None
    #: Per-bar profit factor; ``None`` when unbounded (gains but no losing
    #: bars). The trade-level figure is ``trade_stats.trade_profit_factor``.
    profit_factor: float | None
    equity: list[EquityPoint]
    #: Drawdown from the running peak at each equity point (fraction <= 0).
    drawdown: list[EquityPoint] = Field(default_factory=list)
    #: Closed round trips (``trade_stats.n_trades``).
    trade_count: int = 0
    trade_stats: TradeStatsView | None = None
    trades: list[TradeView] = Field(default_factory=list)
    sortino: float | None = None
    calmar: float | None = None
    ulcer_index: float | None = None
    max_dd_duration_bars: int = 0
    var_95: float | None = None
    es_95: float | None = None
    skew: float | None = None
    kurtosis: float | None = None
    #: Tulchinsky fitness (Sharpe x sqrt(|return| / turnover)).
    fitness: float | None = None


class LabRunOptions(BaseModel):
    """Everything about a lab run except the strategy and window; shared by
    :class:`LabRunRequest` and the Studio's draft lab runs."""

    train_ratio: float = Field(default=0.7, gt=0, lt=1)
    tuner: TunerName = "random"
    budget: int = Field(default=20, ge=1, le=1_000)
    seed: int = 0
    objective: ObjectiveName = "sharpe"
    #: Points per numeric axis for the ``grid`` tuner.
    grid_size: int = Field(default=5, ge=1, le=50)
    #: Survival test ids to run, in order. When omitted, ``preset`` decides.
    survival_tests: list[SurvivalTestName] | None = Field(default=None, min_length=1)
    #: A named suite used when ``survival_tests`` is omitted. Without either,
    #: registering runs use ``promotion`` and other runs ``quick``.
    preset: SurvivalPresetName | None = None
    #: Settings for the ``walk_forward`` survival test (defaults when omitted).
    walk_forward: WalkForwardConfig | None = None
    #: Settings for the ``mcpt`` (alias ``permutation``) survival test.
    mcpt: McptOptions | None = None
    #: Always register the fitted strategy (status ``shadow``) with its
    #: reports, whatever the verdict.
    register_strategy: bool = False
    #: Register the fitted strategy only when every survival test passes.
    register_if_passes: bool = False
    #: A preset from ``GET /api/lab/cost-models`` or explicit cost-model
    #: settings for every backtest of the run; default ``[backtest.costs]``.
    cost_model: CostModelOption | None = None
    #: What edge the strategy exploits and who pays for it (P1); recorded
    #: in the trial ledger before tuning and in the artifact's ``meta.json``.
    hypothesis: str | None = Field(default=None, max_length=4_000)
    #: How the strategy is expected to fail; recorded like ``hypothesis``.
    premortem: str | None = Field(default=None, max_length=4_000)

    @property
    def registers(self) -> bool:
        return self.register_strategy or self.register_if_passes

    def suite(self) -> list[str]:
        """Survival test ids to run: explicit tests (legacy names mapped),
        else ``preset``, else ``promotion`` when registering, else ``quick``."""
        tests = [_LEGACY_TEST_NAMES.get(t, t) for t in self.survival_tests or []]
        default = "promotion" if self.registers else "quick"
        return resolve_suite(tests, preset=self.preset, default=default)

    @model_validator(mode="after")
    def _check_options(self) -> Self:
        if self.register_strategy and self.register_if_passes:
            raise ValueError("set register_strategy (always) or register_if_passes, not both")
        if self.walk_forward is None and self.mcpt is None:
            return self
        suite = self.suite()
        if self.walk_forward is not None and "walk_forward" not in suite:
            raise ValueError("walk_forward options given: add 'walk_forward' to survival_tests")
        if self.mcpt is not None and "mcpt" not in suite:
            raise ValueError("mcpt options given: add 'permutation' to survival_tests")
        return self


class LabRunRequest(_WindowRequest, LabRunOptions):
    """Tunes the class the ``strategy`` ref points at (its ``params`` are
    ignored: the tuner searches the class's parameter space)."""


class LabRunView(BaseModel):
    class_path: str
    best_params: dict[str, Any]
    best_score: float | None
    verdict: Literal["pass", "fail"]
    survival_reports: list[SurvivalReportView]
    registered_strategy_id: str | None
    #: The run's id in the trial ledger (``lab_runs``).
    run_id: str = ""
    #: Tuning trials this run evaluated.
    n_trials_run: int = 0
    #: Trials of this strategy class across every ledgered run (P2).
    n_trials_class: int = 0


@dataclass(frozen=True)
class LabExecution:
    """What :func:`execute_lab_run` returns."""

    result: LabRunResult
    registered_id: str | None

    def view(self) -> LabRunView:
        result = self.result
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
            registered_strategy_id=self.registered_id,
            run_id=result.run_id,
            n_trials_run=result.n_trials_run,
            n_trials_class=result.n_trials_class,
        )


def build_tuner(options: LabRunOptions, parallel: ParallelSettings | None = None) -> Tuner:
    """The request's tuner, spreading trials over ``parallel`` workers."""
    if options.tuner == "grid":
        return GridTuner(grid_size=options.grid_size, seed=options.seed, parallel=parallel)
    return RandomTuner(seed=options.seed, parallel=parallel)


def lab_costs(settings: Any, option: CostModelOption | None) -> CostModelSettings:
    """The request's cost model, else the configured ``[backtest.costs]``."""
    return _cost_settings(option) if option is not None else settings.backtest.costs


def execute_lab_run(
    settings: Any,
    cls: type[Strategy],
    request: LabRunRequest,
    *,
    lake: Any,
    state: Any = None,
    progress: JobContext | None = None,
    register: RegisterFn | None = None,
    fixed_params: Mapping[str, Any] | None = None,
    parallel: ParallelSettings | None = None,
) -> LabExecution:
    """Tune ``cls`` on ``lake`` and run the survival suite; the one lab-run
    code path (see the module doc).

    ``state`` (an open ``SqliteState``) enables the trial ledger and is
    required to register. ``register`` replaces the plain registry
    registration (the Studio links its draft too); with
    ``register_if_passes`` neither runs on a failed verdict. ``progress``
    adds job progress and cancellation checks between trials and tests.
    ``parallel`` defaults to ``[lab.parallel]``. ``fixed_params`` pin
    parameters for the whole search (``--params`` on the CLI)."""
    interval = _parse_interval(request.interval)
    tuner = build_tuner(request, parallel or settings.lab.parallel)
    objective: Objective = _OBJECTIVES[request.objective]()
    tests: list[SurvivalTest] = [
        build_survival_test(name, _test_options(name, request, settings.lab.walk_forward))
        for name in request.suite()
    ]
    if progress is not None:
        objective = _CancellableObjective(objective, progress)
        tests = [_CancellableTest(t, progress) for t in tests]
    ledger = TrialLedger(state, settings.registry.artifacts_dir) if state is not None else None
    runner = LabRunner(
        tuner=tuner,
        objective=objective,
        suite=SurvivalSuite(tests),
        budget=request.budget,
        ledger=ledger,
        settings=settings,
    )
    if progress is not None:
        progress.progress(0.05, "tuning")
    dataset = LabDataset(
        lake=lake,
        universe=list(request.universe),
        start=request.start,
        end=request.end,
        train_ratio=request.train_ratio,
        interval=interval,
        costs=lab_costs(settings, request.cost_model),
    )
    result = runner.run(
        cls,
        dataset,
        fixed_params=fixed_params,
        hypothesis=request.hypothesis,
        premortem=request.premortem,
    )

    registered: str | None = None
    if request.register_if_passes and result.verdict != "pass":
        _log.info(
            "lab.not_registered", run_id=result.run_id, reason="verdict", verdict=result.verdict
        )
    elif request.registers:
        if state is None:
            raise ValueError("registering a lab run needs the state store")
        if progress is not None:
            progress.progress(0.95, "registering")
        if register is not None:
            registered = register(result.strategy, list(result.survival_reports))
        else:
            registry = StrategyRegistry(state=state, artifacts_dir=settings.registry.artifacts_dir)
            registered = registry.register(result.strategy, result.survival_reports)
        _attach_lab_meta(state, registered, result)
        _log.info(
            "lab.registered", run_id=result.run_id, strategy_id=registered, verdict=result.verdict
        )
    return LabExecution(result=result, registered_id=registered)


def _attach_lab_meta(state: Any, strategy_id: str, result: LabRunResult) -> None:
    """Merge the run's provenance (run id, trial count, hypothesis,
    manifest) into the registered artifact's ``meta.json``."""
    rows = state.sql("SELECT artifact_path FROM strategies WHERE id = ?", [strategy_id])
    if not rows:  # a custom register hook that registered elsewhere
        _log.warning("lab.meta.no_artifact", strategy_id=strategy_id)
        return
    update_meta(rows[0]["artifact_path"], result.artifact_meta)


def backtest_report(
    settings: Any, strategy: Strategy, request: Any, lake: Any
) -> tuple[BacktestReport, Interval]:
    """Backtest ``strategy`` for a request carrying a window and
    :class:`BacktestOptions`; the report has its trade ledger attached."""
    interval = _parse_interval(request.interval)
    cost_model = _backtest_cost_model(settings, request)
    if _backtest_costs_zero(settings, request):
        _log.warning(
            "backtest.zero_costs",
            strategy=getattr(strategy, "id", type(strategy).__name__),
            hint="results ignore fees, spread and impact; pass a cost model",
        )
    broker = SimulatedBroker(
        portfolio=Portfolio(cash=request.initial_cash, positions={}),
        slippage_bps=request.slippage_bps,
        fee_per_trade=request.fee_per_trade,
        cost_model=cost_model,
    )
    config = BacktestConfig(
        start=request.start,
        end=request.end,
        universe=list(request.universe),
        interval=interval,
        threshold=request.threshold,
        rebalance_every_bars=request.rebalance_every_bars,
    )
    report = Backtester(strategies=[strategy], broker=broker, lake=lake, config=config).run()
    report = with_trades(report, broker.fills, reference_price=broker.reference_price)
    return report, interval


def backtest_result(report: BacktestReport, interval: Interval, request: Any) -> BacktestResult:
    """The API view of a backtest report (equity, drawdown, trades, stats)."""
    stamps = [_as_datetime(ts) for ts in report.equity_dates]
    stats = report.trade_stats
    return BacktestResult(
        strategy_id=report.strategy_id,
        interval=interval.code,
        start=request.start,
        end=request.end,
        final_return=finite(report.final_return),
        sharpe=finite(report.sharpe),
        max_drawdown=finite(report.max_drawdown),
        cagr=finite(report.cagr),
        profit_factor=finite(report.bar_profit_factor),
        equity=[
            EquityPoint(timestamp=ts, value=float(v))
            for ts, v in zip(stamps, report.equity_curve, strict=True)
        ],
        drawdown=[
            EquityPoint(timestamp=ts, value=float(dd))
            for ts, dd in zip(stamps, bt_metrics.drawdowns(report.equity_curve), strict=True)
        ],
        trade_count=stats.n_trades,
        trade_stats=TradeStatsView(
            n_trades=stats.n_trades,
            n_open=stats.n_open,
            win_rate=stats.win_rate,
            avg_win=stats.avg_win,
            avg_loss=stats.avg_loss,
            payoff_ratio=finite(stats.payoff_ratio),
            expectancy=stats.expectancy,
            trade_profit_factor=finite(stats.trade_profit_factor),
            avg_bars_held=stats.avg_bars_held,
            exposure=stats.exposure,
            turnover_annual=stats.turnover_annual,
            costs_paid=stats.costs_paid,
            cost_drag_annual=stats.cost_drag_annual,
        ),
        trades=[
            TradeView(
                ticker=t.ticker,
                entry_ts=t.entry_ts,
                exit_ts=t.exit_ts,
                qty=t.qty,
                entry_px=t.entry_px,
                exit_px=t.exit_px,
                pnl=t.pnl,
                return_pct=t.return_pct,
                bars_held=t.bars_held,
                fees=t.fees,
                is_open=t.is_open,
            )
            for t in report.trades
        ],
        sortino=finite(report.sortino),
        calmar=finite(report.calmar),
        ulcer_index=finite(report.ulcer_index),
        max_dd_duration_bars=report.max_dd_duration_bars,
        var_95=finite(report.var_95),
        es_95=finite(report.es_95),
        skew=finite(report.skew),
        kurtosis=finite(report.kurtosis),
        fitness=finite(report.fitness),
    )


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
        return self.run_backtest_strategy(self._strategies.resolve(request.strategy), request)

    def run_backtest_strategy(self, strategy: Strategy, request: Any) -> BacktestResult:
        """Backtest an already-built strategy (the Studio's code drafts) with
        a request's window and :class:`BacktestOptions`."""
        with self._ctx.lake() as lake:
            report, interval = backtest_report(self._ctx.settings, strategy, request, lake)
        return backtest_result(report, interval, request)

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
        """Run :func:`execute_lab_run` for ``cls`` (``request.strategy`` is
        not resolved here) on this context's lake and state."""
        with self._ctx.lake() as lake, self._ctx.state() as state:
            execution = execute_lab_run(
                self._ctx.settings,
                cls,
                request,
                lake=lake,
                state=state,
                progress=progress,
                register=register,
            )
        return execution.view()

    # ---- job handlers ------------------------------------------------------

    def _handle_backtest(self, params: dict[str, Any], ctx: JobContext) -> BacktestResult:
        return self.run_backtest(BacktestRequest.model_validate(params))

    def _handle_lab_run(self, params: dict[str, Any], ctx: JobContext) -> LabRunView:
        return self.run_lab(LabRunRequest.model_validate(params), progress=ctx)


class _CancellableObjective:
    """Objective wrapper that honours job cancellation before every trial
    (including the re-tuning trials of walk-forward / MCPT).

    On the parallel tuning path the pool ships ``worker_objective`` (the
    picklable inner objective) to the workers and runs ``checkpoint`` in
    this process after each trial, so API lab runs tune in parallel and
    stay cancellable."""

    def __init__(self, inner: Objective, ctx: JobContext) -> None:
        self._inner = inner
        self._ctx = ctx
        self.name = inner.name
        self.direction = inner.direction
        if callable(getattr(inner, "evaluate", None)):
            self.evaluate = self._evaluate

    @property
    def worker_objective(self) -> Objective:
        return self._inner

    def checkpoint(self) -> None:
        self._ctx.check_cancelled()

    def score(self, strategy: Any, dataset: Any) -> float:
        self._ctx.check_cancelled()
        return self._inner.score(strategy, dataset)

    def _evaluate(self, strategy: Any, dataset: Any) -> Any:
        self._ctx.check_cancelled()
        return self._inner.evaluate(strategy, dataset)  # type: ignore[attr-defined]


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


def _test_options(
    name: str, request: LabRunOptions, walk_forward_default: WalkForwardConfig
) -> dict[str, Any] | None:
    """Registry options for test ``name``: request options win; walk-forward
    otherwise uses ``[lab.walk_forward]``; other tests use their defaults."""
    if name == "walk_forward":
        return {"config": request.walk_forward or walk_forward_default}
    if name == "mcpt" and request.mcpt is not None:
        return request.mcpt.model_dump()
    return None


def _backtest_cost_model(settings: Any, request: Any) -> CostModel | None:
    """A named preset, else flat slippage / fee when given (``None``: the
    broker charges those), else the configured ``[backtest.costs]``."""
    if request.cost_model is not None:
        return _cost_settings(request.cost_model).build()
    if request.slippage_bps or request.fee_per_trade:
        return None
    return settings.backtest.costs.build()


def _backtest_costs_zero(settings: Any, request: Any) -> bool:
    if request.cost_model is not None:
        return costs_are_zero(_cost_settings(request.cost_model))
    if request.slippage_bps or request.fee_per_trade:
        return False
    return costs_are_zero(settings.backtest.costs)


def _cost_settings(option: CostModelName | CostModelSettings) -> CostModelSettings:
    if isinstance(option, CostModelSettings):
        return option
    return _COST_MODELS[option][1]()


def _parse_interval(code: str) -> Interval:
    try:
        return Interval.parse(code)
    except (ValueError, TypeError) as exc:
        raise ValidationError(f"invalid interval {code!r}: {exc}") from None


def _as_datetime(value: date | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime(value.year, value.month, value.day)
