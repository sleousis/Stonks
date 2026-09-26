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
from stonks.app.errors import ConflictError, NotFoundError, ValidationError
from stonks.app.jobs import Job, JobContext, JobRunner
from stonks.app.serialize import FiniteFloat, finite, to_jsonable
from stonks.app.strategies import StrategyRef, StrategyService, SurvivalReportView
from stonks.backtest import metrics as bt_metrics
from stonks.backtest.benchmark import BenchmarkResult, benchmark_curve, with_benchmark
from stonks.backtest.costs import CostModel, CostModelSettings
from stonks.backtest.engine import BacktestConfig, Backtester
from stonks.backtest.report import BacktestReport
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.backtest.trades import with_trades
from stonks.core.interval import Interval
from stonks.core.protocols import Objective, Strategy, SurvivalReport, SurvivalTest, Tuner
from stonks.core.types import Portfolio
from stonks.ingest.ensure import DataEnsurer, EnsureReport
from stonks.ingest.wiring import build_ingest_pipeline
from stonks.lab.backtesting import run_backtest
from stonks.lab.dataset import LabDataset, scoring_window
from stonks.lab.objectives import CAGRObjective, FinalReturnObjective, SharpeObjective
from stonks.lab.parallel import ParallelSettings
from stonks.lab.preflight import PreflightError, PreflightReport
from stonks.lab.runner import LabRunner, LabRunResult, costs_are_zero
from stonks.lab.survival import registry as survival_registry
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
from stonks.lab.universe_data import prepare_dataset
from stonks.logging import get_logger
from stonks.registry.artifact import update_meta
from stonks.registry.store import StrategyRegistry
from stonks.universes.base import UNIVERSE_ID_PATTERN
from stonks.universes.store import UniverseStore

BACKTEST_JOB = "backtest"
LAB_RUN_JOB = "lab_run"
#: The chained job that fetches a lab run's missing bars (``ensure_data``).
LAB_ENSURE_JOB = "lab_ensure"

TunerName = Literal["grid", "random"]
ObjectiveName = Literal["sharpe", "cagr", "final_return"]
CostModelName = Literal["zero", "realistic"]

#: API names kept from before the survival-test registry (BL-10).
_LEGACY_TEST_NAMES: dict[str, str] = {"permutation": "mcpt"}
#: Constructor arguments that take objects the lab builds (a tuning setup,
#: a settings model), not JSON options.
_NOT_OPTIONS = frozenset({"config", "tuning"})


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
#: A benchmark spec (``stonks.backtest.benchmark.normalize_spec``): ``auto``
#: (SPY.US when priced, else the equal-weight universe), ``EW``, a ticker
#: such as ``QQQ.US``, or ``none``. Omitted: ``[lab] benchmark``.
BenchmarkSpec = Annotated[
    str,
    Field(
        max_length=32,
        description="auto (SPY.US when priced, else EW), EW (equal-weight universe), "
        "a ticker such as QQQ.US, or none; default [lab] benchmark",
    ),
]
#: Options per survival test id, e.g. ``{"oos": {"mode": "sharpe"}}``.
SurvivalTestOptions = dict[SurvivalTestName, dict[str, Any]]

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
    #: What the result is compared against (``BacktestResult.benchmark``).
    benchmark: BenchmarkSpec | None = None

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

    n_permutations: int = Field(default=200, ge=1, le=1_000)
    max_p_value: float = Field(default=0.05, gt=0, le=1)
    metric: Literal["profit_factor", "sharpe", "final_return", "cagr"] = "profit_factor"
    #: Re-tune on every permutation (Masters); costs ``(n + 1) * budget``
    #: backtests. ``"auto"`` re-tunes only a strategy with a non-trivial
    #: ``fit`` (the promotion preset's choice).
    retune: bool | Literal["auto"] = False
    seed: int | None = 17


class EquityPoint(BaseModel):
    timestamp: datetime
    value: float


class BenchmarkStatsView(BaseModel):
    """A backtest against its benchmark (``backtest.benchmark.BenchmarkStats``).
    Ratios are ``None`` when not finite."""

    #: Display name (a ticker or ``EW``).
    name: str
    #: The spec it was resolved from (``auto``, ``EW``, a ticker).
    spec: str
    #: Paired return observations the stats use.
    n_obs: int
    benchmark_cagr: FiniteFloat
    #: Strategy CAGR minus benchmark CAGR.
    excess_cagr: FiniteFloat
    benchmark_sharpe: FiniteFloat
    benchmark_max_dd: FiniteFloat
    beta: FiniteFloat
    alpha_annual: FiniteFloat
    #: HAC (Newey-West) t-statistic of the alpha.
    alpha_tstat: FiniteFloat
    r2: FiniteFloat
    residual_sharpe: FiniteFloat
    tracking_error: FiniteFloat
    information_ratio: FiniteFloat
    up_capture: FiniteFloat
    down_capture: FiniteFloat
    correlation: FiniteFloat
    #: Names held by an equal-weight benchmark.
    members: list[str] = Field(default_factory=list)
    #: Universe names left out (not priced at the first date).
    excluded: list[str] = Field(default_factory=list)

    @classmethod
    def of(cls, result: BenchmarkResult) -> BenchmarkStatsView:
        stats = {k: v for k, v in result.metrics().items() if k != "n_obs"}
        return cls(
            name=result.curve.name,
            spec=result.curve.spec,
            n_obs=int(result.stats.n_obs),
            members=list(result.curve.members),
            excluded=list(result.curve.excluded),
            **stats,
        )


class TradeStatsView(BaseModel):
    """Trade-level statistics of a backtest (``backtest.trades.TradeStats``).
    Win/loss figures are over closed round trips; ``None`` marks an
    unbounded ratio (no losing trades)."""

    n_trades: int
    n_open: int
    win_rate: FiniteFloat
    avg_win: FiniteFloat
    avg_loss: FiniteFloat
    payoff_ratio: FiniteFloat
    #: Mean P&L per closed trade, in cash.
    expectancy: FiniteFloat
    trade_profit_factor: FiniteFloat
    avg_bars_held: FiniteFloat
    #: Share of bars that closed with a position held.
    exposure: FiniteFloat
    turnover_annual: FiniteFloat
    costs_paid: FiniteFloat
    cost_drag_annual: FiniteFloat


class TradeView(BaseModel):
    """One round trip (a lot, or part of one, from buy to sell or to the end)."""

    ticker: str
    entry_ts: datetime
    exit_ts: datetime
    qty: float
    entry_px: float
    exit_px: float
    pnl: float
    return_pct: FiniteFloat
    bars_held: int
    fees: float
    is_open: bool


class BacktestResult(BaseModel):
    strategy_id: str
    interval: str
    start: date
    end: date
    final_return: FiniteFloat
    sharpe: FiniteFloat
    max_drawdown: FiniteFloat
    cagr: FiniteFloat
    #: Per-bar profit factor; ``None`` when unbounded (gains but no losing
    #: bars). The trade-level figure is ``trade_stats.trade_profit_factor``.
    profit_factor: FiniteFloat
    equity: list[EquityPoint]
    #: Drawdown from the running peak at each equity point (fraction <= 0).
    drawdown: list[EquityPoint] = Field(default_factory=list)
    #: Closed round trips (``trade_stats.n_trades``).
    trade_count: int = 0
    trade_stats: TradeStatsView | None = None
    trades: list[TradeView] = Field(default_factory=list)
    sortino: FiniteFloat = None
    calmar: FiniteFloat = None
    ulcer_index: FiniteFloat = None
    max_dd_duration_bars: int = 0
    var_95: FiniteFloat = None
    es_95: FiniteFloat = None
    skew: FiniteFloat = None
    kurtosis: FiniteFloat = None
    #: Tulchinsky fitness (Sharpe x sqrt(|return| / turnover)).
    fitness: FiniteFloat = None
    #: The strategy against its benchmark (``benchmark`` request option,
    #: default ``[lab] benchmark``); ``None`` when off or unpriced.
    benchmark: BenchmarkStatsView | None = None
    #: The benchmark's buy-and-hold value on the strategy's equity
    #: timestamps, starting at the same capital.
    benchmark_equity: list[EquityPoint] = Field(default_factory=list)


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
    #: Options per survival test id, validated by the test's ``Options``
    #: model (or its constructor), e.g. ``{"oos": {"mode": "sharpe",
    #: "min_trades": 0}, "pbo": {"max_pbo": 0.3}}``. Each test must be in
    #: the resolved suite. Applied over ``walk_forward`` / ``mcpt``.
    test_options: SurvivalTestOptions | None = None
    #: Benchmark for the run's backtests (``benchmark_relative``, the
    #: result's ``benchmark``); default ``[lab] benchmark``.
    benchmark: BenchmarkSpec | None = None
    #: Trading bars skipped between the train and validation windows
    #: (BL-20); a strategy's ``label_horizon_bars`` raises it. Default
    #: ``[lab] embargo_bars``.
    embargo_bars: int | None = Field(default=None, ge=0, le=10_000)
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
    #: Run the BL-37 data preflight first; default ``[lab] preflight``.
    preflight: bool | None = None
    #: Treat preflight warnings as errors; default ``[lab] strict_preflight``.
    strict_preflight: bool | None = None

    @property
    def registers(self) -> bool:
        return self.register_strategy or self.register_if_passes

    def suite(self) -> list[str]:
        """Survival test ids to run: explicit tests (legacy names mapped),
        else ``preset``, else ``promotion`` when registering, else ``quick``."""
        tests = [_LEGACY_TEST_NAMES.get(t, t) for t in self.survival_tests or []]
        default = "promotion" if self.registers else "quick"
        return resolve_suite(tests, preset=self.preset, default=default)

    def options_preset(self) -> str | None:
        """The preset whose test options apply: ``preset`` when named, else
        the default suite's (``promotion`` when registering, else
        ``quick``) unless explicit ``survival_tests`` replaced it."""
        if self.preset:
            return self.preset
        if self.survival_tests:
            return None
        return "promotion" if self.registers else "quick"

    def survival_options(self, name: str) -> dict[str, Any] | None:
        """Registry options for survival test ``name``: the preset's
        options (``PRESET_OPTIONS``, see :meth:`options_preset`), then the
        ``walk_forward`` / ``mcpt`` fields (the values they set), then
        ``test_options`` (legacy names mapped). ``None`` when nothing sets
        any."""
        preset = self.options_preset()
        options = survival_registry.preset_options(preset).get(name, {}) if preset else {}
        if name == "walk_forward" and self.walk_forward is not None:
            options["config"] = self.walk_forward
        if name == "mcpt" and self.mcpt is not None:
            options.update(self.mcpt.model_dump(exclude_unset=True))
        for key, value in (self.test_options or {}).items():
            if _LEGACY_TEST_NAMES.get(key, key) == name:
                options.update(value)
        return options or None

    @model_validator(mode="after")
    def _check_options(self) -> Self:
        if self.register_strategy and self.register_if_passes:
            raise ValueError("set register_strategy (always) or register_if_passes, not both")
        if self.walk_forward is None and self.mcpt is None and not self.test_options:
            return self
        suite = self.suite()
        if self.walk_forward is not None and "walk_forward" not in suite:
            raise ValueError("walk_forward options given: add 'walk_forward' to survival_tests")
        if self.mcpt is not None and "mcpt" not in suite:
            raise ValueError("mcpt options given: add 'permutation' to survival_tests")
        for key, given in (self.test_options or {}).items():
            name = _LEGACY_TEST_NAMES.get(key, key)
            reserved = sorted(set(given) & _NOT_OPTIONS)
            if reserved:
                raise ValueError(
                    f"test_options for {key!r} can't set {reserved}: those are objects the "
                    "lab builds (use the walk_forward field for walk-forward settings)"
                )
            if name not in suite:
                raise ValueError(
                    f"test_options given for {key!r}, which is not in the suite {suite}: "
                    "add it to survival_tests or pick a preset that runs it"
                )
            # Build once to validate: unknown option names and bad values fail here.
            survival_registry.build_survival_test(name, self.survival_options(name))
        return self


class LabRunRequest(_WindowRequest, LabRunOptions):
    """Tunes the class the ``strategy`` ref points at (its ``params`` are
    ignored: the tuner searches the class's parameter space).

    Give ``universe`` (tickers), ``universe_id`` (a stored universe: every
    member on any day of the window, delisted names included), or both
    (the preflight then reports members missing from the list)."""

    universe: list[str] = Field(default_factory=list)
    #: A stored universe (``/api/universes``) to take the tickers from.
    universe_id: str | None = Field(default=None, pattern=UNIVERSE_ID_PATTERN)
    #: Fetch the missing bars first (window plus the strategy's warm-up),
    #: as a chained ``lab_ensure`` job on the lake writer lane.
    ensure_data: bool = False

    @model_validator(mode="after")
    def _has_a_universe(self) -> Self:
        if not self.universe and not self.universe_id:
            raise ValueError("give universe (tickers) or universe_id")
        return self

    @model_validator(mode="after")
    def _embargo_fits_the_window(self) -> Self:
        check_embargo(self, self.embargo_bars)
        return self


def check_embargo(window: Any, embargo_bars: int | None) -> None:
    """Raise ``ValueError`` when ``embargo_bars`` leaves ``window`` (start,
    end, train_ratio, interval) no validation window."""
    if not embargo_bars:
        return
    try:
        interval = Interval.parse(window.interval)
    except (ValueError, TypeError):
        return  # reported by the interval check
    LabDataset(
        lake=None,  # type: ignore[arg-type]
        start=window.start,
        end=window.end,
        train_ratio=window.train_ratio,
        interval=interval,
        embargo_bars=embargo_bars,
    )


class PreflightIssueView(BaseModel):
    #: e.g. ``missing_data``, ``late_start``, ``static_universe``.
    code: str
    severity: Literal["error", "warning"]
    message: str
    details: dict[str, Any] = {}


class PreflightView(BaseModel):
    """The BL-37 data preflight of a lab run. A run only starts with no
    errors, so a result carries warnings (and ``ok`` true)."""

    ok: bool
    #: True when there was no lake to look at.
    skipped: bool
    issues: list[PreflightIssueView]

    @classmethod
    def of(cls, report: PreflightReport) -> PreflightView:
        return cls(
            ok=report.ok,
            skipped=report.skipped,
            issues=[
                PreflightIssueView(
                    code=i.code,
                    severity=i.severity,
                    message=i.message,
                    details=to_jsonable(dict(i.details)),
                )
                for i in report.issues
            ],
        )


class LabRunView(BaseModel):
    class_path: str
    best_params: dict[str, Any]
    best_score: FiniteFloat
    verdict: Literal["pass", "fail"]
    survival_reports: list[SurvivalReportView]
    registered_strategy_id: str | None
    #: The run's id in the trial ledger (``lab_runs``).
    run_id: str = ""
    #: Tuning trials this run evaluated.
    n_trials_run: int = 0
    #: Trials of this strategy class across every ledgered run (P2).
    n_trials_class: int = 0
    #: The fitted strategy against its benchmark over the validation
    #: window; ``None`` when off or unpriced.
    benchmark: BenchmarkStatsView | None = None
    #: The data preflight's warnings; ``None`` when it was turned off.
    preflight: PreflightView | None = None
    #: The chained ``lab_ensure`` job that fetched missing bars first
    #: (``ensure_data``); its report is at ``/api/lab/ensure/{id}/result``.
    ensure_job_id: str | None = None


@dataclass(frozen=True)
class LabExecution:
    """What :func:`execute_lab_run` returns."""

    result: LabRunResult
    registered_id: str | None
    #: The fitted strategy's validation-window benchmark comparison.
    benchmark: BenchmarkResult | None = None

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
            benchmark=BenchmarkStatsView.of(self.benchmark) if self.benchmark else None,
            preflight=PreflightView.of(result.preflight) if result.preflight else None,
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
    data_ensurer: Any = None,
) -> LabExecution:
    """Tune ``cls`` on ``lake`` and run the survival suite; the one lab-run
    code path (see the module doc).

    ``state`` (an open ``SqliteState``) enables the trial ledger and is
    required to register. ``register`` replaces the plain registry
    registration (the Studio links its draft too); with
    ``register_if_passes`` neither runs on a failed verdict. ``progress``
    adds job progress and cancellation checks between trials and tests.
    ``parallel`` defaults to ``[lab.parallel]``. ``fixed_params`` pin
    parameters for the whole search (``--params`` on the CLI).
    ``data_ensurer`` (a :class:`~stonks.ingest.ensure.DataEnsurer`) fetches
    missing bars before the preflight (``stonks lab run --ensure-data``)."""
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
        preflight=settings.lab.preflight if request.preflight is None else request.preflight,
        strict_preflight=(
            settings.lab.strict_preflight
            if request.strict_preflight is None
            else request.strict_preflight
        ),
        data_ensurer=data_ensurer,
    )
    if progress is not None:
        progress.progress(0.05, "tuning")
    dataset = lab_dataset(settings, request, lake, interval)
    try:
        result = runner.run(
            cls,
            dataset,
            fixed_params=fixed_params,
            hypothesis=request.hypothesis,
            premortem=request.premortem,
        )
    except PreflightError as exc:  # the data can't support the run
        raise ValidationError(str(exc)) from None
    benchmark = _validation_benchmark(result, dataset)

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
    return LabExecution(result=result, registered_id=registered, benchmark=benchmark)


def lab_dataset(
    settings: Any, request: LabRunRequest, lake: Any, interval: Interval | None = None
) -> LabDataset:
    """The request's :class:`LabDataset` on ``lake`` (``ValidationError``
    when the window can't hold it)."""
    embargo = request.embargo_bars
    try:
        return LabDataset(
            lake=lake,
            universe=list(request.universe),
            start=request.start,
            end=request.end,
            train_ratio=request.train_ratio,
            interval=interval or _parse_interval(request.interval),
            costs=lab_costs(settings, request.cost_model),
            benchmark=lab_benchmark(settings, request.benchmark),
            embargo_bars=settings.lab.embargo_bars if embargo is None else embargo,
            execution=settings.backtest.execution,
            construction=settings.backtest.construction,
            universe_id=request.universe_id,
        )
    except ValueError as exc:  # e.g. [lab] embargo_bars leaves no validation window
        raise ValidationError(str(exc)) from None


def build_data_ensurer(settings: Any, lake: Any, source: Any) -> DataEnsurer:
    """A :class:`~stonks.ingest.ensure.DataEnsurer` over ``source`` with the
    ``[ensure]`` settings and the configured ingest pipeline."""
    return DataEnsurer(
        lake,
        source,
        settings.ensure,
        pipeline_factory=lambda src, lk: build_ingest_pipeline(settings, src, lk),
    )


def lab_benchmark(settings: Any, option: str | None) -> str:
    """The request's benchmark spec, else ``[lab] benchmark``."""
    return option if option is not None else settings.lab.benchmark


def _validation_benchmark(result: LabRunResult, dataset: LabDataset) -> BenchmarkResult | None:
    """The fitted strategy against the dataset's benchmark over the
    validation window, embargoed for it (one more backtest; ``None`` when
    off or unpriced)."""
    report = run_backtest(result.strategy, dataset, scoring_window(dataset, result.strategy))
    return getattr(report, "benchmark", None)


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
    execution = settings.backtest.execution
    broker = SimulatedBroker(
        portfolio=Portfolio(cash=request.initial_cash, positions={}),
        slippage_bps=request.slippage_bps,
        fee_per_trade=request.fee_per_trade,
        cost_model=cost_model,
        fill_model=execution.fill_model(),
        settlement_days=execution.settlement_days,
    )
    config = BacktestConfig(
        start=request.start,
        end=request.end,
        universe=list(request.universe),
        interval=interval,
        threshold=request.threshold,
        rebalance_every_bars=request.rebalance_every_bars,
        construction=settings.backtest.construction,
    )
    report = Backtester(strategies=[strategy], broker=broker, lake=lake, config=config).run()
    report = with_trades(report, broker.fills, reference_price=broker.reference_price)
    curve = benchmark_curve(
        lake,
        lab_benchmark(settings, getattr(request, "benchmark", None)),
        report.equity_dates,
        universe=config.universe,
        interval=interval,
        initial_value=report.equity_curve[0] if report.equity_curve else 1.0,
    )
    return with_benchmark(report, curve), interval


def backtest_result(report: BacktestReport, interval: Interval, request: Any) -> BacktestResult:
    """The API view of a backtest report (equity, drawdown, trades, stats)."""
    stamps = [_as_datetime(ts) for ts in report.equity_dates]
    stats = report.trade_stats
    bench: BenchmarkResult | None = getattr(report, "benchmark", None)
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
        benchmark=BenchmarkStatsView.of(bench) if bench is not None else None,
        benchmark_equity=(
            [
                EquityPoint(timestamp=ts, value=float(v))
                for ts, v in zip(stamps, bench.curve.values, strict=True)
            ]
            if bench is not None
            else []
        ),
    )


class LabService:
    def __init__(self, context: AppContext, strategies: StrategyService, runner: JobRunner) -> None:
        self._ctx = context
        self._strategies = strategies
        self._runner = runner
        runner.register(BACKTEST_JOB, self._handle_backtest)
        # Cooperative: stops between tuning trials and survival tests.
        runner.register(LAB_RUN_JOB, self._handle_lab_run, cancellable=True)
        # Fetching a lab run's missing bars writes the lake: one writer lane.
        runner.register(LAB_ENSURE_JOB, self._handle_lab_ensure, lock="lake_write")

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
        if request.universe_id is not None:
            self._require_universe(request.universe_id)
        if request.ensure_data:
            self._ctx.build_source(None)  # fail fast when it isn't configured
        return self._runner.submit(LAB_RUN_JOB, request.model_dump(mode="json"))

    def run_lab(self, request: LabRunRequest, progress: JobContext | None = None) -> LabRunView:
        cls = self._strategies.strategy_class(request.strategy)
        return self.run_lab_class(cls, request, progress=progress)

    def ensure_lab_data(self, request: LabRunRequest) -> EnsureReport:
        """Fetch the bars a lab run will read that the lake lacks: the
        members (or tickers) over the window plus the strategy's warm-up,
        and a benchmark ticker. Runs as the ``lab_ensure`` job."""
        cls = self._strategies.strategy_class(request.strategy)
        source = self._ctx.build_source(None)
        with self._ctx.lake() as lake:
            dataset = lab_dataset(self._ctx.settings, request, lake)
            ensurer = build_data_ensurer(self._ctx.settings, lake, source)
            _, report = prepare_dataset(dataset, ensurer=ensurer, strategy=cls)
        if report is not None:
            return report
        return EnsureReport(  # no tickers to fetch
            interval=request.interval,
            start=request.start,
            end=request.end,
            source=source.source_id,
        )

    def _require_universe(self, universe_id: str) -> None:
        with self._ctx.lake() as lake:
            known = UniverseStore(lake).exists(universe_id) or universe_id in lake.universe_ids()
        if not known:
            raise NotFoundError(f"no universe {universe_id!r}")

    def _ensure_first(self, request: LabRunRequest, ctx: JobContext) -> str:
        """Run the chained ``lab_ensure`` job and wait for it (checking for
        cancellation); its id. A failed ensure fails the lab run."""
        job = self._runner.submit(LAB_ENSURE_JOB, request.model_dump(mode="json"))
        ctx.progress(0.01, f"fetching missing data (job {job.id})")
        while True:
            final = self._runner.wait(job.id, timeout=1.0)
            if final.is_terminal:
                break
            if not self._runner.is_tracked(job.id):
                raise ConflictError(f"data job {job.id} stopped without finishing")
            ctx.check_cancelled()
        if final.status != "succeeded":
            raise ConflictError(f"data job {job.id} {final.status}: {final.error or 'no detail'}")
        return job.id

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
        request = LabRunRequest.model_validate(params)
        ensure_job_id = self._ensure_first(request, ctx) if request.ensure_data else None
        view = self.run_lab(request, progress=ctx)
        return view.model_copy(update={"ensure_job_id": ensure_job_id})

    def _handle_lab_ensure(self, params: dict[str, Any], ctx: JobContext) -> EnsureReport:
        return self.ensure_lab_data(LabRunRequest.model_validate(params))


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
    options = request.survival_options(name)
    if name == "walk_forward":
        return {"config": walk_forward_default, **(options or {})}
    return options


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
