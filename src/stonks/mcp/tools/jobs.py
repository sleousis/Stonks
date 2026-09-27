"""Job tools: queue backtests, lab runs and ingests (research data only,
never orders), plus ``wait_for_job``."""

# No ``from __future__ import annotations`` (see common.py).

import time
from typing import Annotated, Any, Literal

import anyio
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from stonks.mcp.tools.common import (
    JOB,
    JOB_OPEN_WORLD,
    READ,
    Benchmark,
    CostModelName,
    EmbargoBars,
    Hypothesis,
    IsoDate,
    LabCostModel,
    ObjectiveName,
    Premortem,
    RegisterConfirm,
    RegisterIfPasses,
    RegisterStrategy,
    SurvivalPreset,
    SurvivalTestName,
    TestOptions,
    Tickers,
    ToolContext,
    TunerName,
    drop_none,
    iso,
    queue_lab_run,
    seg,
)

TERMINAL_JOB_STATUSES = frozenset({"succeeded", "failed", "cancelled"})

#: Stops work (a destructive act), and stopping twice changes nothing.
CANCEL = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False
)

#: Typed result route per job kind (``{id}`` is the validated job id). A
#: kind missing here (e.g. studio jobs) reports the job's own ``result``.
RESULT_ROUTES: dict[str, str] = {
    "backtest": "/api/lab/backtests/{id}/result",
    "lab_run": "/api/lab/runs/{id}/result",
    "ingest": "/api/ingest/jobs/{id}/result",
    "tick": "/api/ticks/jobs/{id}/result",
    "signal_ic": "/api/lab/signal-ic/{id}/result",
    "factor_tearsheet": "/api/factors/tearsheets/{id}/result",
    "lab_ensure": "/api/lab/ensure/{id}/result",
    "lab_sweep": "/api/lab/sweeps/{id}/result",
    "universe_refresh": "/api/universes/refresh/{id}/result",
    "universe_ensure": "/api/universes/ensure/{id}/result",
}


Metric = Literal["sharpe", "cagr", "final_return"]

OptionalTickers = Annotated[
    list[str] | None, Field(description="instrument ids, or give universe_id")
]
UniverseId = Annotated[
    str | None,
    Field(
        pattern=r"^[a-z0-9][a-z0-9_.-]{0,63}$",
        description="a stored universe (see list_universes): its point-in-time members, "
        "delisted names included, instead of typed tickers",
    ),
]
EnsureData = Annotated[
    bool,
    Field(
        description="fetch the bars the run needs that the lake lacks first (a lab_ensure "
        "job the run waits for, and its id is ensure_job_id in the result)"
    ),
]
Preflight = Annotated[
    bool | None,
    Field(description="check the data before tuning (missing bars, gaps). Default [lab] preflight"),
]
StrictPreflight = Annotated[
    bool | None,
    Field(description="treat preflight warnings as errors. Default [lab] strict_preflight"),
]


def _need_universe(universe: list[str] | None, universe_id: str | None) -> None:
    if not universe and not universe_id:
        raise ToolError("give universe (tickers) or universe_id")


class _Options(BaseModel):
    """Lab-run option block. Unset fields are not sent, so the API's
    request models own the defaults and the bounds."""

    model_config = ConfigDict(extra="forbid")

    def body(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class WalkForwardOptions(_Options):
    """Settings of the ``walk_forward`` survival test."""

    n_splits: int | None = Field(default=None, description="number of folds (default 4)")
    test_days: int | None = Field(
        default=None, description="days per test window; default splits the validation window"
    )
    train_days: int | None = Field(
        default=None, description="days per train window; default all history before it"
    )
    anchored: bool | None = Field(default=None, description="expanding (true) or rolling window")
    metric: Metric | None = Field(default=None, description="statistic scored per test window")
    min_positive_share: float | None = Field(
        default=None, description="share of folds that must score above 0 (0..1)"
    )
    min_mean_score: float | None = Field(default=None, description="mean fold score floor")
    min_wfe: float | None = Field(
        default=None, description="walk-forward efficiency gate, OOS / IS return (default 0.5)"
    )
    matrix: bool | None = Field(
        default=None, description="also run the train x test matrix (default false)"
    )


class McptOptions(_Options):
    """Settings of the ``permutation`` (Monte-Carlo permutation) survival test."""

    n_permutations: int | None = Field(default=None, description="1..1000 (default 200)")
    max_p_value: float | None = Field(default=None, description="pass threshold (default 0.05)")
    metric: Literal["profit_factor", "sharpe", "final_return", "cagr"] | None = None
    retune: bool | Literal["auto"] | None = Field(
        default=None,
        description="re-tune on every permutation: (n + 1) x budget backtests; 'auto' re-tunes "
        "only strategies with a non-trivial fit (the promotion preset's default)",
    )
    seed: int | None = None


def strategy_ref(
    strategy_id: str | None, class_path: str | None, params: dict[str, Any] | None
) -> dict[str, Any]:
    if (strategy_id is None) == (class_path is None):
        raise ToolError("set exactly one of strategy_id or class_path")
    return drop_none({"strategy_id": strategy_id, "class_path": class_path, "params": params})


def register(t: ToolContext) -> None:
    server = t.server
    max_wait = t.max_wait_seconds

    @server.tool(annotations=JOB)
    async def run_backtest(
        start: IsoDate,
        end: IsoDate,
        universe: OptionalTickers = None,
        universe_id: UniverseId = None,
        strategy_id: Annotated[
            str | None, Field(description="registered strategy id (or use class_path)")
        ] = None,
        class_path: Annotated[
            str | None, Field(description="catalog class path, e.g. pkg.mod:Class")
        ] = None,
        params: Annotated[
            dict[str, Any] | None, Field(description="strategy params (with class_path)")
        ] = None,
        interval: str = "1d",
        initial_cash: Annotated[float, Field(gt=0)] = 10_000.0,
        threshold: float = 0.0,
        rebalance_every_bars: Annotated[int, Field(ge=1)] = 1,
        slippage_bps: Annotated[float, Field(ge=0)] = 0.0,
        fee_per_trade: Annotated[float, Field(ge=0)] = 0.0,
        cost_model: Annotated[
            CostModelName | None,
            Field(
                description="transaction-cost preset (see list_cost_models); replaces "
                "slippage_bps/fee_per_trade. Neither: the configured [backtest.costs]"
            ),
        ] = None,
        benchmark: Benchmark = None,
    ) -> dict[str, Any]:
        """Queue a backtest of one strategy over typed tickers (universe) or a
        stored universe (universe_id) and a date window. Returns the job. Use
        wait_for_job to get the metrics and equity curve. Simulated only:
        never places real orders."""
        _need_universe(universe, universe_id)
        body = drop_none(
            {
                "strategy": strategy_ref(strategy_id, class_path, params),
                "universe": universe,
                "universe_id": universe_id,
                "start": iso(start),
                "end": iso(end),
                "interval": interval,
                "initial_cash": initial_cash,
                "threshold": threshold,
                "rebalance_every_bars": rebalance_every_bars,
                "slippage_bps": slippage_bps,
                "fee_per_trade": fee_per_trade,
                "cost_model": cost_model,
                "benchmark": benchmark,
            }
        )
        return await t.post("/api/lab/backtests", body)

    @server.tool(annotations=JOB)
    async def run_lab(
        class_path: Annotated[str, Field(description="catalog class path to tune")],
        start: IsoDate,
        end: IsoDate,
        universe: OptionalTickers = None,
        universe_id: UniverseId = None,
        ensure_data: EnsureData = False,
        preflight: Preflight = None,
        strict_preflight: StrictPreflight = None,
        survival_tests: Annotated[
            list[SurvivalTestName] | None,
            Field(description="survival suite; server default when omitted"),
        ] = None,
        tuner: TunerName = "random",
        objective: ObjectiveName = "sharpe",
        budget: Annotated[int, Field(ge=1, le=1000, description="tuner trials")] = 20,
        train_ratio: Annotated[float, Field(gt=0, lt=1)] = 0.7,
        interval: str = "1d",
        seed: int = 0,
        register_strategy: RegisterStrategy = False,
        register_if_passes: RegisterIfPasses = False,
        confirm: RegisterConfirm = False,
        preset: SurvivalPreset = None,
        cost_model: LabCostModel = None,
        hypothesis: Hypothesis = None,
        premortem: Premortem = None,
        walk_forward: Annotated[
            WalkForwardOptions | None,
            Field(description="walk_forward test settings; add 'walk_forward' to survival_tests"),
        ] = None,
        mcpt: Annotated[
            McptOptions | None,
            Field(description="permutation (MCPT) settings; add 'permutation' to survival_tests"),
        ] = None,
        test_options: TestOptions = None,
        benchmark: Benchmark = None,
        embargo_bars: EmbargoBars = None,
    ) -> dict[str, Any]:
        """Queue a lab run: tune a strategy class, fit, run the survival suite and
        give a pass/fail verdict. Returns the job; use wait_for_job for the result.
        Every run and trial is recorded in the trial ledger (with the hypothesis).
        Give typed tickers (universe) or a stored universe (universe_id), and
        ensure_data=true to fetch missing bars first.
        Registering (register_strategy, or register_if_passes to register only a
        passing run) needs confirm=true (preview otherwise)."""
        _need_universe(universe, universe_id)
        body = drop_none(
            {
                "strategy": {"class_path": class_path},
                "universe": universe,
                "universe_id": universe_id,
                "ensure_data": ensure_data or None,
                "preflight": preflight,
                "strict_preflight": strict_preflight,
                "start": iso(start),
                "end": iso(end),
                "survival_tests": survival_tests,
                "tuner": tuner,
                "objective": objective,
                "budget": budget,
                "train_ratio": train_ratio,
                "interval": interval,
                "seed": seed,
                "register_strategy": register_strategy,
                "register_if_passes": register_if_passes or None,
                "preset": preset,
                "cost_model": cost_model,
                "hypothesis": hypothesis,
                "premortem": premortem,
                "walk_forward": walk_forward.body() if walk_forward else None,
                "mcpt": mcpt.body() if mcpt else None,
                "test_options": test_options,
                "benchmark": benchmark,
                "embargo_bars": embargo_bars,
            }
        )
        return await queue_lab_run(t, "/api/lab/runs", body, confirm)

    @server.tool(annotations=JOB)
    async def run_sweep(
        start: IsoDate,
        end: IsoDate,
        universe: OptionalTickers = None,
        universe_id: UniverseId = None,
        strategies: Annotated[
            list[str] | None,
            Field(
                max_length=200,
                description="strategy ids (e.g. momentum), class names or module:Class. "
                "default every catalogued strategy",
            ),
        ] = None,
        exclude: Annotated[
            list[str] | None, Field(max_length=200, description="strategies to leave out")
        ] = None,
        preset: SurvivalPreset = None,
        survival_tests: Annotated[
            list[SurvivalTestName] | None,
            Field(description="survival suite for every strategy, the server default when omitted"),
        ] = None,
        tuner: TunerName = "random",
        objective: ObjectiveName = "sharpe",
        budget: Annotated[int, Field(ge=1, le=1000, description="tuner trials each")] = 20,
        train_ratio: Annotated[float, Field(gt=0, lt=1)] = 0.7,
        interval: str = "1d",
        seed: int = 0,
        cost_model: LabCostModel = None,
        benchmark: Benchmark = None,
        embargo_bars: EmbargoBars = None,
        preflight: Preflight = None,
        strict_preflight: StrictPreflight = None,
        hypothesis: Hypothesis = None,
        premortem: Premortem = None,
    ) -> dict[str, Any]:
        """Queue a sweep: a lab run of every strategy (or the ones named) on
        the same tickers or stored universe and window, ranked best first.
        A strategy with a ticker parameter runs once per ticker. Returns the
        job, and wait_for_job gives the ranked rows. Every trial is counted in
        the trial ledger. A sweep never registers a strategy: register the
        one you like with run_lab."""
        _need_universe(universe, universe_id)
        body = drop_none(
            {
                "universe": universe,
                "universe_id": universe_id,
                "strategies": strategies,
                "exclude": exclude,
                "start": iso(start),
                "end": iso(end),
                "preset": preset,
                "survival_tests": survival_tests,
                "tuner": tuner,
                "objective": objective,
                "budget": budget,
                "train_ratio": train_ratio,
                "interval": interval,
                "seed": seed,
                "cost_model": cost_model,
                "benchmark": benchmark,
                "embargo_bars": embargo_bars,
                "preflight": preflight,
                "strict_preflight": strict_preflight,
                "hypothesis": hypothesis,
                "premortem": premortem,
            }
        )
        return await t.post("/api/lab/sweeps", body)

    @server.tool(annotations=CANCEL)
    async def cancel_job(job_id: str) -> dict[str, Any]:
        """Stop one of your background jobs: a queued job never starts, and a
        running lab run stops at its next trial (it ends cancelled). Other
        running jobs cannot be interrupted (409). Ticks and ingests need an
        admin."""
        return await t.post(f"/api/jobs/{seg(job_id)}/cancel")

    @server.tool(annotations=JOB)
    async def run_signal_ic(
        universe: Tickers,
        start: IsoDate,
        end: IsoDate,
        strategy_id: Annotated[
            str | None, Field(description="registered strategy id (or use class_path)")
        ] = None,
        class_path: Annotated[
            str | None, Field(description="catalog class path, e.g. pkg.mod:Class")
        ] = None,
        params: Annotated[
            dict[str, Any] | None, Field(description="strategy params (with class_path)")
        ] = None,
        interval: str = "1d",
        horizons: Annotated[
            list[int] | None,
            Field(description="forward-return horizons in bars; default [1, 5, 21]"),
        ] = None,
        every_bars: Annotated[int, Field(ge=1, description="score every N bars")] = 5,
        n_quantiles: Annotated[int, Field(ge=2, le=20)] = 5,
    ) -> dict[str, Any]:
        """Queue a signal IC analysis: does the strategy's estimate_return rank
        future returns across the universe? Reports mean IC, ICIR, HAC t-stat
        and quantile spread per horizon, plus turnover and the IC estimate.
        Needs at least 10 tickers (else n/a). Returns the job; use wait_for_job
        for the result. Research only: writes nothing."""
        body = drop_none(
            {
                "strategy": strategy_ref(strategy_id, class_path, params),
                "universe": universe,
                "start": iso(start),
                "end": iso(end),
                "interval": interval,
                "horizons": horizons,
                "every_bars": every_bars,
                "n_quantiles": n_quantiles,
            }
        )
        return await t.post("/api/lab/signal-ic", body)

    @server.tool(annotations=JOB_OPEN_WORLD)
    async def run_ingest(
        kind: Literal["prices", "intraday", "fundamentals", "metadata"],
        tickers: Annotated[
            list[str] | None, Field(description="instrument ids; or use exchange")
        ] = None,
        exchange: Annotated[str | None, Field(description="ingest a whole exchange")] = None,
        since: IsoDate | None = None,
        until: IsoDate | None = None,
        interval: Annotated[str | None, Field(description="for intraday, e.g. 5m")] = None,
    ) -> dict[str, Any]:
        """Queue a market-data ingest into the lake from the configured vendor.
        Returns the job; use wait_for_job for the outcome."""
        body = drop_none(
            {
                "kind": kind,
                "tickers": tickers,
                "exchange": exchange,
                "since": iso(since),
                "until": iso(until),
                "interval": interval,
            }
        )
        return await t.post("/api/ingest/runs", body)

    @server.tool(annotations=READ)
    async def wait_for_job(
        job_id: str,
        timeout_seconds: Annotated[
            float, Field(gt=0, description=f"give up after this long (max {max_wait:g})")
        ] = 120.0,
        poll_seconds: Annotated[float, Field(ge=0.05, le=30)] = 1.0,
    ) -> dict[str, Any]:
        """Poll a job until it finishes or the timeout passes. Returns
        {"timed_out": bool, "job": {...}, "result": {...} | null}: once the job
        succeeded, "result" is its typed result (BacktestResult, LabRunView,
        SignalICView, ingest or tick result); otherwise null and the job carries its error."""
        jid = seg(job_id)
        deadline = time.monotonic() + min(timeout_seconds, max_wait)
        while True:
            job = await t.get(f"/api/jobs/{jid}")
            if job.get("status") in TERMINAL_JOB_STATUSES:
                return {"timed_out": False, "job": job, "result": await typed_result(jid, job)}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {"timed_out": True, "job": job, "result": None}
            await anyio.sleep(min(poll_seconds, remaining))

    async def typed_result(jid: str, job: dict[str, Any]) -> Any:
        if job.get("status") != "succeeded":
            return None
        route = RESULT_ROUTES.get(str(job.get("kind")))
        if route is None:
            return job.get("result")
        return await t.get(route.format(id=jid))
