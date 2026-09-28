"""OptionsService: options research for the console and MCP (roadmap 17.6).

Reads stored chains with our implied vol and Greeks (the
:class:`~stonks.options.pricing.PricingModel` seam), lists the options
strategies and structures, draws a structure's expiry payoff, and runs an
options backtest with its validation as a background job.

Research only: nothing here places an order or writes to the lake. The
job writes nothing but its own row.
"""

from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Any, Literal, Self

from pydantic import BaseModel, Field, model_validator

from stonks.app.catalog import ParameterInfo
from stonks.app.context import AppContext
from stonks.app.errors import NotFoundError, ValidationError
from stonks.app.jobs import Job, JobContext, JobRunner
from stonks.app.serialize import FiniteFloat, to_jsonable
from stonks.app.strategies import SurvivalReportView
from stonks.options.analytics import analyze
from stonks.options.chain import ChainSnapshot, OptionQuote
from stonks.options.payoff import MAX_DTE, payoff_structures, structure_payoff
from stonks.options.pricing import default_model_for
from stonks.options.store import OptionStore, UnderlyingSummary
from stonks.options.strategies import option_strategy_catalog
from stonks.options.strategy import OptionStrategy

OPTIONS_BACKTEST_JOB = "options_backtest"

#: The source id of generated chains, which are never evidence.
SYNTHETIC_SOURCE = "synthetic"
#: The expiry a chain shows when none is asked for: nearest this many days.
DEFAULT_CHAIN_DTE = 30
MAX_UNDERLYINGS = 10

Ticker = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9._\-]+$")

# ---- views ---------------------------------------------------------------------------


class OptionUnderlyingView(BaseModel):
    underlying: str
    first_day: date
    last_day: date
    #: Days with a stored chain.
    days: int
    contracts: int
    sources: list[str]
    #: Generated chains only: fine for trying the tools, never evidence.
    synthetic: bool


class OptionQuoteView(BaseModel):
    contract_id: str
    bid: FiniteFloat = None
    ask: FiniteFloat = None
    #: The mid, else the last trade.
    mark: FiniteFloat = None
    volume: FiniteFloat = None
    open_interest: FiniteFloat = None
    #: Our implied vol: the vendor's when it sent one, else solved from the mark.
    iv: FiniteFloat = None
    delta: FiniteFloat = None
    gamma: FiniteFloat = None
    #: Money per share per calendar day.
    theta: FiniteFloat = None
    #: Money per share per vol point.
    vega: FiniteFloat = None


class OptionChainRow(BaseModel):
    strike: float
    call: OptionQuoteView | None = None
    put: OptionQuoteView | None = None


class OptionChainView(BaseModel):
    underlying: str
    #: The chain's day: the last stored day on or before the one asked for.
    as_of: date
    spot: FiniteFloat = None
    #: Every expiry listed that day.
    expiries: list[date]
    #: The expiry the rows show.
    expiry: date | None = None
    days_to_expiry: int | None = None
    rows: list[OptionChainRow]
    #: Pricing models behind the Greeks.
    models: list[str]
    synthetic: bool


class OptionStrategyView(BaseModel):
    id: str
    hypothesis: str
    structures: list[str]
    parameters: list[ParameterInfo]


class OptionStructureView(BaseModel):
    name: str
    #: The parameters it reads (``dte``, ``delta``, ``long_delta``, ...).
    params: list[str]
    #: Its payoff includes one contract's worth of shares held.
    holds_shares: bool


_Delta = Field(default=None, gt=0.0, lt=1.0)


class OptionPayoffRequest(BaseModel):
    underlying: str = Ticker
    #: The chain's day (the last stored day on or before it); latest if unset.
    as_of: date | None = None
    structure: str = Field(min_length=1, max_length=64)
    dte: int = Field(default=35, ge=1, le=MAX_DTE)
    delta: float | None = _Delta
    long_delta: float | None = _Delta
    short_delta: float | None = _Delta
    wing_delta: float | None = _Delta

    def params(self) -> dict[str, float]:
        raw = {
            "dte": self.dte,
            "delta": self.delta,
            "long_delta": self.long_delta,
            "short_delta": self.short_delta,
            "wing_delta": self.wing_delta,
        }
        return {k: v for k, v in raw.items() if v is not None}


class PayoffLegView(BaseModel):
    instrument: str
    kind: Literal["option", "shares"]
    right: Literal["call", "put"] | None = None
    strike: float | None = None
    expiry: date | None = None
    #: Signed: contracts for an option, shares for stock. Negative is short.
    quantity: float
    #: Per share, now (the option's mark or the stock's close).
    price: float


class PayoffPointView(BaseModel):
    #: Underlying price at expiry.
    spot: float
    profit: float


class OptionPayoffView(BaseModel):
    underlying: str
    as_of: date
    spot: float
    structure: str
    legs: list[PayoffLegView]
    points: list[PayoffPointView]
    #: Paid to open: positive for a debit, negative for a credit.
    cost: float
    #: ``null`` when the loss has no bound.
    max_loss: FiniteFloat = None
    #: ``null`` when the gain has no bound.
    max_gain: FiniteFloat = None
    breakevens: list[float]
    synthetic: bool


class OptionsBacktestRequest(BaseModel):
    strategy: str = Field(min_length=1, max_length=64)
    underlyings: list[str] = Field(min_length=1, max_length=MAX_UNDERLYINGS)
    start: date
    end: date
    cash: float = Field(default=100_000.0, gt=0, le=1e10)
    params: dict[str, Any] = Field(default_factory=dict[str, Any])
    #: Also run the validation checks (out of sample, deflated Sharpe,
    #: wider fills, missing quote days, doubled fees).
    validation: bool = True
    #: Trials run so far on this strategy, for the deflated Sharpe.
    trials: int = Field(default=1, ge=1, le=100_000)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.start > self.end:
            raise ValueError("start must be on or before end")
        cleaned = [t.strip() for t in self.underlyings if t.strip()]
        if not cleaned:
            raise ValueError("name at least one underlying")
        self.underlyings = list(dict.fromkeys(cleaned))
        return self


class EquityPointView(BaseModel):
    date: date
    value: float


class OptionsBacktestView(BaseModel):
    strategy: str
    underlyings: list[str]
    start: date
    end: date
    days: int
    final_return: FiniteFloat = None
    sharpe: FiniteFloat = None
    max_drawdown: FiniteFloat = None
    cagr: FiniteFloat = None
    #: Combo fills.
    fills: int
    rejected: int
    #: Why combos were not filled, with counts.
    rejection_reasons: dict[str, int]
    equity: list[EquityPointView]
    sources: list[str]
    synthetic: bool
    validation: list[SurvivalReportView]
    #: ``passed`` when every check passed, ``not_run`` without validation.
    verdict: Literal["passed", "failed", "not_run"]


# ---- helpers -------------------------------------------------------------------------


def _underlying_view(s: UnderlyingSummary) -> OptionUnderlyingView:
    return OptionUnderlyingView(
        underlying=s.underlying,
        first_day=s.first_day,
        last_day=s.last_day,
        days=s.days,
        contracts=s.contracts,
        sources=list(s.sources),
        synthetic=set(s.sources) == {SYNTHETIC_SOURCE},
    )


def _quote_view(quote: OptionQuote, spot: float | None) -> OptionQuoteView:
    a = analyze(quote, spot)
    g = a.greeks
    return OptionQuoteView(
        contract_id=quote.contract_id,
        bid=quote.bid,
        ask=quote.ask,
        mark=a.mark,
        volume=quote.volume,
        open_interest=quote.open_interest,
        iv=a.iv,
        delta=g.delta if g else None,
        gamma=g.gamma if g else None,
        theta=g.theta_per_day if g else None,
        vega=g.vega_per_point if g else None,
    )


def _nearest_expiry(chain: ChainSnapshot, dte: int) -> date | None:
    listed = [e for e in chain.expiries() if e >= chain.as_of]
    if not listed:
        return None
    return min(listed, key=lambda e: (abs((e - chain.as_of).days - dte), e))


def _reason_kind(reason: str) -> str:
    """A rejection reason without its contract ids and amounts, so counts group."""
    if reason.startswith("no two-sided quote"):
        return "no two-sided quote"
    if reason.startswith("no price"):
        return "no underlying price"
    if "beyond the limit" in reason:
        return "net price beyond the limit"
    if reason.startswith("needs"):
        return "not enough cash"
    return reason.split(" for ")[0][:80]


def _strategy_view(cls: type[OptionStrategy]) -> OptionStrategyView:
    return OptionStrategyView(
        id=cls.id,
        hypothesis=cls.hypothesis,
        structures=list(cls.structures),
        parameters=[
            ParameterInfo(
                name=spec.name,
                kind=spec.kind,
                default=to_jsonable(spec.default),
                bounds=None if spec.bounds is None else to_jsonable(list(spec.bounds)),
                tunable=spec.tunable,
                description=spec.description,
            )
            for spec in cls.parameter_spec()
        ],
    )


# ---- the service ---------------------------------------------------------------------


class OptionsService:
    def __init__(self, context: AppContext, runner: JobRunner | None = None) -> None:
        self._ctx = context
        self._runner = runner
        if runner is not None:
            runner.register(OPTIONS_BACKTEST_JOB, self._handle_backtest)

    # ---- reads ---------------------------------------------------------------------

    def underlyings(self) -> list[OptionUnderlyingView]:
        """Underlyings with stored chains, and the days they cover."""
        with self._ctx.lake() as lake:
            return [_underlying_view(s) for s in OptionStore(lake).underlyings()]

    def _snapshot(self, store: OptionStore, underlying: str, as_of: date | None) -> ChainSnapshot:
        day = store.latest_day(underlying, as_of)
        if day is None:
            when = f" on or before {as_of}" if as_of else ""
            raise NotFoundError(f"no stored option chain for {underlying}{when}")
        return store.chain(underlying, day)

    def chain(
        self, underlying: str, *, as_of: date | None = None, expiry: date | None = None
    ) -> OptionChainView:
        """One expiry of a stored chain with our implied vol and Greeks,
        calls and puts side by side by strike."""
        with self._ctx.lake() as lake:
            store = OptionStore(lake)
            snap = self._snapshot(store, underlying, as_of)
            sources = store.sources([underlying], snap.as_of, snap.as_of)
        expiries = snap.expiries()
        if expiry is not None and expiry not in expiries:
            raise NotFoundError(
                f"{underlying} lists no {expiry} expiry on {snap.as_of}; "
                f"listed: {', '.join(str(e) for e in expiries)}"
            )
        shown = expiry or _nearest_expiry(snap, DEFAULT_CHAIN_DTE)
        by_strike: dict[float, OptionChainRow] = {}
        models: set[str] = set()
        for quote in snap.filter(expiry=shown) if shown else []:
            row = by_strike.setdefault(
                quote.contract.strike, OptionChainRow(strike=quote.contract.strike)
            )
            view = _quote_view(quote, snap.spot)
            if quote.contract.right == "call":
                row.call = view
            else:
                row.put = view
            models.add(default_model_for(quote.contract).name)
        return OptionChainView(
            underlying=underlying,
            as_of=snap.as_of,
            spot=snap.spot,
            expiries=expiries,
            expiry=shown,
            days_to_expiry=(shown - snap.as_of).days if shown else None,
            rows=[by_strike[k] for k in sorted(by_strike)],
            models=sorted(models),
            synthetic=set(sources) == {SYNTHETIC_SOURCE},
        )

    def strategies(self) -> list[OptionStrategyView]:
        """The options strategy catalog with each hypothesis and parameters."""
        return [_strategy_view(cls) for cls in option_strategy_catalog().values()]

    def structures(self) -> list[OptionStructureView]:
        """Structures a payoff can be drawn for."""
        return [
            OptionStructureView(name=s.name, params=list(s.params), holds_shares=s.holds_shares)
            for s in payoff_structures()
        ]

    def payoff(self, request: OptionPayoffRequest) -> OptionPayoffView:
        """One unit of a structure picked from a stored chain, and its
        profit at expiry over a range of underlying prices."""
        names = {s.name for s in payoff_structures()}
        if request.structure not in names:
            raise ValidationError(
                f"unknown structure {request.structure!r}; choose one of {sorted(names)}"
            )
        with self._ctx.lake() as lake:
            store = OptionStore(lake)
            snap = self._snapshot(store, request.underlying, request.as_of)
            sources = store.sources([request.underlying], snap.as_of, snap.as_of)
        built = structure_payoff(request.structure, snap, request.params())
        if built is None or snap.spot is None:
            raise ValidationError(
                f"the {snap.as_of} chain of {request.underlying} has no liquid legs for "
                f"{request.structure} near {request.dte} days"
            )
        p = built.payoff
        return OptionPayoffView(
            underlying=request.underlying,
            as_of=snap.as_of,
            spot=snap.spot,
            structure=request.structure,
            legs=[
                PayoffLegView(
                    instrument=leg.contract.contract_id if leg.contract else str(leg.shares),
                    kind="option" if leg.contract else "shares",
                    right=leg.contract.right if leg.contract else None,
                    strike=leg.contract.strike if leg.contract else None,
                    expiry=leg.contract.expiry if leg.contract else None,
                    quantity=leg.quantity,
                    price=leg.price,
                )
                for leg in built.legs
            ],
            points=[PayoffPointView(spot=s, profit=v) for s, v in p.points],
            cost=p.cost,
            max_loss=p.max_loss,
            max_gain=p.max_gain,
            breakevens=list(p.breakevens),
            synthetic=set(sources) == {SYNTHETIC_SOURCE},
        )

    # ---- backtests -----------------------------------------------------------------

    def _strategy(self, request: OptionsBacktestRequest) -> type[OptionStrategy]:
        catalog = option_strategy_catalog()
        cls = catalog.get(request.strategy)
        if cls is None:
            raise ValidationError(
                f"unknown options strategy {request.strategy!r}; choose one of {sorted(catalog)}"
            )
        try:
            cls(request.params or None)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"bad parameters for {request.strategy}: {exc}") from None
        return cls

    def submit_backtest(
        self, request: OptionsBacktestRequest, *, owner_id: str | None = None
    ) -> Job:
        if self._runner is None:  # pragma: no cover - wiring error
            raise RuntimeError("OptionsService has no job runner")
        self._strategy(request)
        with self._ctx.lake() as lake:
            sources = OptionStore(lake).sources(request.underlyings, request.start, request.end)
        if not sources:
            raise ValidationError(
                f"no stored option chains for {', '.join(request.underlyings)} between "
                f"{request.start} and {request.end}; load them with stonks options ingest"
            )
        return self._runner.submit(
            OPTIONS_BACKTEST_JOB, request.model_dump(mode="json"), owner_id=owner_id
        )

    def run_backtest(
        self, request: OptionsBacktestRequest, progress: JobContext | None = None
    ) -> OptionsBacktestView:
        from stonks.backtest.options_engine import (
            OptionMarketData,
            OptionsBacktestConfig,
            OptionsBacktester,
        )
        from stonks.options.validation import (
            OptionValidationSettings,
            validate_option_strategy,
        )

        cls = self._strategy(request)
        params = request.params or None
        with self._ctx.lake() as lake:
            data = OptionMarketData.from_lake(lake, request.underlyings, request.start, request.end)
            sources = OptionStore(lake).sources(request.underlyings, request.start, request.end)
        config = OptionsBacktestConfig(
            start=request.start,
            end=request.end,
            underlyings=request.underlyings,
            initial_cash=request.cash,
        )
        if progress is not None:
            progress.progress(0.1, f"backtesting {request.strategy}")
        result = OptionsBacktester(cls(params), data, config).run()
        reports: list[SurvivalReportView] = []
        if request.validation:
            if progress is not None:
                progress.progress(0.5, "running the validation checks")
            reports = [
                SurvivalReportView(
                    test_id=r.test_id, passed=r.passed, metrics=dict(r.metrics), notes=r.notes
                )
                for r in validate_option_strategy(
                    cls, params, data, config, OptionValidationSettings(n_trials=request.trials)
                )
            ]
        r = result.report
        verdict: Literal["passed", "failed", "not_run"] = (
            "not_run" if not reports else "passed" if all(x.passed for x in reports) else "failed"
        )
        return OptionsBacktestView(
            strategy=request.strategy,
            underlyings=request.underlyings,
            start=request.start,
            end=request.end,
            days=r.n_bars,
            final_return=r.final_return,
            sharpe=r.sharpe,
            max_drawdown=r.max_drawdown,
            cagr=r.cagr,
            fills=result.n_fills,
            rejected=len(result.rejections),
            rejection_reasons=dict(Counter(_reason_kind(x.reason) for x in result.rejections)),
            equity=[
                EquityPointView(date=d, value=v)
                for d, v in zip(r.equity_dates, r.equity_curve, strict=True)
            ],
            sources=sources,
            synthetic=set(sources) == {SYNTHETIC_SOURCE},
            validation=reports,
            verdict=verdict,
        )

    def _handle_backtest(self, params: dict[str, Any], ctx: JobContext) -> OptionsBacktestView:
        return self.run_backtest(OptionsBacktestRequest.model_validate(params), progress=ctx)
