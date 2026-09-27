"""FactorService: the factor library, formula checks, values at a date and
factor tear sheet jobs (roadmap 22.2, 22.3, 22.8).

Research only: reads the lake and writes nothing but the job row and the
panel cache (``[factors]``).
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any, Self

import pandas as pd
from pydantic import BaseModel, Field, field_validator, model_validator

from stonks.app.context import AppContext
from stonks.app.errors import NotFoundError, ValidationError
from stonks.app.jobs import Job, JobContext, JobRunner
from stonks.core.interval import Interval
from stonks.factors.base import Factor
from stonks.factors.engine import PanelRequest
from stonks.factors.expression import ExpressionError
from stonks.factors.panels import FactorEngine
from stonks.factors.registry import factor_catalog, factor_sets, get_factor, resolve_factor
from stonks.factors.settings import make_panel_cache
from stonks.factors.tearsheet import FactorTearSheet, TearSheetOptions, factor_tearsheet

FACTOR_TEARSHEET_JOB = "factor_tearsheet"

MAX_HORIZON_BARS = 504
MAX_UNIVERSE = 5000

# ---- views ---------------------------------------------------------------------------


class FactorView(BaseModel):
    id: str
    kind: str
    family: str
    #: The library module it comes from (``alpha158``, ``classic``,
    #: ``fundamentals``); ``None`` for a formula typed by a person.
    set: str | None = None
    description: str
    #: ``1``: higher should earn more; ``-1``: lower should.
    direction: int
    hypothesis: str
    #: Canonical formula of an expression factor.
    expression: str | None = None
    lookback_bars: int
    asset_classes: list[str]


class FactorSetView(BaseModel):
    name: str
    count: int


class FactorCatalogView(BaseModel):
    factors: list[FactorView]
    sets: list[FactorSetView]
    families: list[str]


class ExpressionCheckRequest(BaseModel):
    expression: str = Field(min_length=1, max_length=2000)


class ExpressionCheckView(BaseModel):
    ok: bool
    #: The formula in canonical form (its id as an ad hoc factor).
    canonical: str | None = None
    lookback_bars: int | None = None
    error: str | None = None


class _UniverseMixin(BaseModel):
    #: Tickers; or name a stored universe with ``universe_id``.
    universe: list[str] | None = Field(default=None, min_length=1, max_length=MAX_UNIVERSE)
    universe_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def _one_universe(self) -> Self:
        if (self.universe is None) == (self.universe_id is None):
            raise ValueError("give exactly one of universe or universe_id")
        return self


class FactorValuesRequest(_UniverseMixin):
    #: A library factor id or a formula.
    factor: str = Field(min_length=1, max_length=2000)
    as_of: date


class FactorValue(BaseModel):
    ticker: str
    value: float
    #: 1 is best in the factor's direction.
    rank: int


class FactorValuesView(BaseModel):
    factor_id: str
    as_of: str
    direction: int
    #: Best first in the factor's direction.
    values: list[FactorValue]
    #: Universe names with no value on the date.
    missing: list[str]


class FactorTearSheetRequest(_UniverseMixin):
    factor: str = Field(min_length=1, max_length=2000)
    start: date
    end: date
    interval: str = "1d"
    #: Forward-return horizons in bars.
    horizons: list[int] = Field(default=[1, 5, 21], min_length=1, max_length=20)
    #: Sample every this many bars of the window.
    every_bars: int = Field(default=5, ge=1, le=MAX_HORIZON_BARS)
    n_quantiles: int = Field(default=5, ge=2, le=20)
    #: Fewest names with a value and a return for a date's IC.
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


class HorizonSummaryView(BaseModel):
    horizon: int
    n_dates: int
    mean_ic: float | None
    ic_std: float | None
    icir: float | None
    hit_rate: float | None
    t_stat_hac: float | None
    hac_lags: int
    #: Mean forward return per bucket, lowest values first.
    quantile_means: list[float | None]
    spread_mean: float | None
    spread_t_hac: float | None


class GroupICView(BaseModel):
    group: str
    n_dates: int
    mean_names: float | None
    mean_ic: float | None
    t_stat_hac: float | None


class QuantileCurvesView(BaseModel):
    dates: list[str] = Field(default_factory=list)
    series: list[list[float | None]] = Field(default_factory=list)
    spread: list[float | None] = Field(default_factory=list)


class AlphaBetaView(BaseModel):
    n_periods: int = 0
    alpha_annual: float | None = None
    alpha_t: float | None = None
    beta: float | None = None
    r_squared: float | None = None
    benchmark: str = "equal_weight"


class MonthlyICView(BaseModel):
    year: int
    months: list[float | None]


class FactorTearSheetView(BaseModel):
    """:class:`stonks.factors.tearsheet.FactorTearSheet`; NaN is null."""

    factor: FactorView
    window: tuple[str, str]
    interval: str
    universe_id: str | None = None
    n_tickers: int
    n_dates: int
    every_bars: int
    n_quantiles: int
    #: ``ok`` or ``n/a`` (see ``note``).
    status: str
    note: str = ""
    coverage: float | None = None
    horizons: list[HorizonSummaryView] = Field(default_factory=list)
    ic_horizon: int | None = None
    ic_series: list[tuple[str, float | None]] = Field(default_factory=list)
    #: Keys ``sector``, ``asset_class`` and ``size``.
    ic_by_group: dict[str, list[GroupICView]] = Field(default_factory=dict)
    #: ``market_cap``, ``dollar_volume`` or ``none``.
    size_basis: str = "none"
    quantile_curves: QuantileCurvesView = Field(default_factory=QuantileCurvesView)
    alpha_beta: AlphaBetaView = Field(default_factory=AlphaBetaView)
    monthly_ic: list[MonthlyICView] = Field(default_factory=list)
    score_turnover: float | None = None
    top_quantile_turnover: float | None = None


# ---- helpers -------------------------------------------------------------------------


def _interval(code: str) -> Interval:
    try:
        return Interval.parse(code)
    except (ValueError, TypeError) as exc:
        raise ValidationError(f"invalid interval {code!r}: {exc}") from None


def _set_of() -> dict[str, str]:
    return {fid: name for name, ids in factor_sets().items() for fid in ids}


def factor_view(factor: Factor, set_name: str | None = None) -> FactorView:
    return FactorView.model_validate(factor.to_dict() | {"set": set_name})


def _resolve(text: str) -> Factor:
    try:
        return resolve_factor(text)
    except ExpressionError as exc:
        raise ValidationError(f"not a library factor or a valid formula: {exc}") from None


class FactorService:
    def __init__(self, context: AppContext, runner: JobRunner | None = None) -> None:
        self._ctx = context
        settings = context.settings
        self.cache = make_panel_cache(settings.factors, settings.lake.path)
        if runner is not None:
            runner.register(FACTOR_TEARSHEET_JOB, self._handle_tearsheet)
        self._runner = runner

    # ---- catalog -------------------------------------------------------------------

    def catalog(
        self, *, family: str | None = None, set: str | None = None, kind: str | None = None
    ) -> FactorCatalogView:
        """The library, optionally narrowed to one family, set or kind."""
        sets = factor_sets()
        if set is not None and set not in sets:
            raise NotFoundError(f"unknown factor set {set!r}; choose from {sorted(sets)}")
        owner = _set_of()
        chosen = [
            f
            for f in factor_catalog().values()
            if (family is None or f.family == family)
            and (kind is None or f.kind == kind)
            and (set is None or owner.get(f.id) == set)
        ]
        return FactorCatalogView(
            factors=[factor_view(f, owner.get(f.id)) for f in chosen],
            sets=[FactorSetView(name=name, count=len(ids)) for name, ids in sets.items()],
            families=sorted({f.family for f in factor_catalog().values()}),
        )

    def get(self, factor_id: str) -> FactorView:
        try:
            factor = get_factor(factor_id)
        except ValueError:
            raise NotFoundError(f"unknown factor {factor_id!r}") from None
        return factor_view(factor, _set_of().get(factor.id))

    def check_expression(self, request: ExpressionCheckRequest) -> ExpressionCheckView:
        """Whether a formula parses and is point in time, and its warm-up."""
        try:
            factor = resolve_factor(request.expression)
        except ExpressionError as exc:
            return ExpressionCheckView(ok=False, error=str(exc))
        canonical = getattr(factor, "expression", None) or factor.id
        return ExpressionCheckView(ok=True, canonical=canonical, lookback_bars=factor.lookback_bars)

    # ---- values --------------------------------------------------------------------

    def values(self, request: FactorValuesRequest) -> FactorValuesView:
        """Each universe name's value known at the close of ``as_of``."""
        factor = _resolve(request.factor)
        with self._ctx.lake() as lake:
            tickers = self._tickers_at(lake, request, request.as_of)
            got = factor.values_at(lake, tickers, datetime.combine(request.as_of, time()))
        ordered = sorted(got, key=lambda t: (-factor.direction * got[t], t))
        return FactorValuesView(
            factor_id=factor.id,
            as_of=request.as_of.isoformat(),
            direction=factor.direction,
            values=[FactorValue(ticker=t, value=got[t], rank=i + 1) for i, t in enumerate(ordered)],
            missing=[t for t in tickers if t not in got],
        )

    # ---- tear sheets ---------------------------------------------------------------

    def submit_tearsheet(
        self, request: FactorTearSheetRequest, *, owner_id: str | None = None
    ) -> Job:
        if self._runner is None:  # pragma: no cover - wiring error
            raise RuntimeError("FactorService has no job runner")
        _interval(request.interval)
        _resolve(request.factor)  # validate before queueing
        return self._runner.submit(
            FACTOR_TEARSHEET_JOB, request.model_dump(mode="json"), owner_id=owner_id
        )

    def compute_tearsheet(self, request: FactorTearSheetRequest) -> FactorTearSheet:
        factor = _resolve(request.factor)
        interval = _interval(request.interval)
        options = TearSheetOptions(
            horizons=tuple(request.horizons),
            every_bars=request.every_bars,
            n_quantiles=request.n_quantiles,
            min_names=request.min_names,
        )
        with self._ctx.lake() as lake:
            panel_request = self._panel_request(lake, request, interval)
            return factor_tearsheet(
                factor, lake, panel_request, options, engine=FactorEngine(lake, self.cache)
            )

    def run_tearsheet(self, request: FactorTearSheetRequest) -> FactorTearSheetView:
        sheet = self.compute_tearsheet(request)
        payload = sheet.to_dict()
        payload["factor"] = factor_view(_resolve(request.factor), _set_of().get(sheet.factor["id"]))
        return FactorTearSheetView.model_validate(payload)

    def _handle_tearsheet(self, params: dict[str, Any], ctx: JobContext) -> FactorTearSheetView:
        return self.run_tearsheet(FactorTearSheetRequest.model_validate(params))

    # ---- universes -----------------------------------------------------------------

    def _known_universe(self, lake: Any, universe_id: str) -> None:
        if universe_id not in lake.universe_ids():
            raise NotFoundError(f"unknown universe {universe_id!r} (no membership rows)")

    def _tickers_at(self, lake: Any, request: _UniverseMixin, day: date) -> list[str]:
        if request.universe is not None:
            return list(dict.fromkeys(request.universe))
        assert request.universe_id is not None
        self._known_universe(lake, request.universe_id)
        return lake.members_as_of(request.universe_id, day)

    def _panel_request(
        self, lake: Any, request: FactorTearSheetRequest, interval: Interval
    ) -> PanelRequest:
        if request.universe is not None:
            return PanelRequest(tuple(request.universe), request.start, request.end, interval)
        assert request.universe_id is not None
        self._known_universe(lake, request.universe_id)
        tickers = lake.members_between(request.universe_id, request.start, request.end)
        if not tickers:
            raise ValidationError(f"universe {request.universe_id!r} has no members in the window")
        spans = lake.get_universe_membership(request.universe_id)
        membership = pd.DataFrame(spans[["ticker", "start_date", "end_date"]])
        return PanelRequest(
            tuple(tickers),
            request.start,
            request.end,
            interval,
            universe_id=request.universe_id,
            membership=membership,
        )
