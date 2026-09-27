"""Tax settings, specific-lot picks and yearly tax exports per portfolio,
and FX rate reads (roadmap 20.5). Every portfolio is one of yours (404
otherwise). CSV routes answer ``text/csv`` as an attachment."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel

from stonks.api.deps import PortfolioIdDep, PrincipalDep, ServicesDep, client_ip, needs
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.app.errors import ValidationError
from stonks.app.tax import (
    LotPicksUpdate,
    LotPickView,
    TaxService,
    TaxSettingsUpdate,
    TaxSettingsView,
)
from stonks.auth import Permission

router = APIRouter(prefix="/api/tax", tags=["tax"], responses=PROBLEM_RESPONSES)
fx_router = APIRouter(prefix="/api/fx", tags=["fx"], responses=PROBLEM_RESPONSES)

_CSV: dict[int | str, dict[str, Any]] = {
    200: {
        "description": "A CSV file with a header row",
        "content": {"text/csv": {"schema": {"type": "string", "format": "binary"}}},
    }
}

Year = Annotated[int, Query(ge=1900, le=2200, description="Calendar year, e.g. 2025")]


def _service(services: ServicesDep) -> TaxService:
    return TaxService(services.context)


def _csv(body: str, name: str) -> Response:
    return Response(
        content=body.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/settings", response_model=TaxSettingsView, operation_id="getTaxSettings")
def get_tax_settings(services: ServicesDep, portfolio_id: PortfolioIdDep) -> TaxSettingsView:
    """Base currency, jurisdiction, lot method and wash sale switch of one
    of your portfolios (defaults when never set)."""
    return _service(services).settings(portfolio_id)


@router.put(
    "/settings",
    response_model=TaxSettingsView,
    operation_id="updateTaxSettings",
    dependencies=needs(Permission.PORTFOLIO_MANAGE),
)
def update_tax_settings(
    services: ServicesDep,
    principal: PrincipalDep,
    portfolio_id: PortfolioIdDep,
    body: TaxSettingsUpdate,
    request: Request,
) -> TaxSettingsView:
    """Change the fields you send. Audited."""
    return _service(services).update_settings(principal, portfolio_id, body, ip=client_ip(request))


@router.get("/lots/picks", response_model=list[LotPickView], operation_id="listTaxLotPicks")
def list_tax_lot_picks(
    services: ServicesDep, portfolio_id: PortfolioIdDep, sell_fill_id: int | None = None
) -> list[LotPickView]:
    """The specific lots each sell closes (used with lot_method specific)."""
    return _service(services).picks(portfolio_id, sell_fill_id)


@router.put(
    "/lots/picks",
    response_model=list[LotPickView],
    operation_id="setTaxLotPicks",
    dependencies=needs(Permission.PORTFOLIO_MANAGE),
)
def set_tax_lot_picks(
    services: ServicesDep,
    principal: PrincipalDep,
    portfolio_id: PortfolioIdDep,
    body: LotPicksUpdate,
    request: Request,
) -> list[LotPickView]:
    """Replace the lots one sell fill closes (an empty list clears them).
    The buys must be this portfolio's, the same ticker, filled before the
    sell, and fit the sell's and each buy's quantity. Audited."""
    return _service(services).set_picks(principal, portfolio_id, body, ip=client_ip(request))


@router.get(
    "/exports/gains",
    operation_id="exportTaxGains",
    response_class=Response,
    responses=_CSV,
    summary="Realized gains per lot as CSV",
)
def export_tax_gains(services: ServicesDep, portfolio_id: PortfolioIdDep, year: Year) -> Response:
    """Every lot disposed in ``year``: FIFO or your specific lots, US wash
    sales when on, amounts in the trade and the base currency."""
    body = _service(services).gains_csv(portfolio_id, year)
    return _csv(body, f"gains-{portfolio_id}-{year}.csv")


@router.get(
    "/exports/dividends",
    operation_id="exportTaxDividends",
    response_class=Response,
    responses=_CSV,
    summary="Dividends and withholding as CSV",
)
def export_tax_dividends(
    services: ServicesDep, portfolio_id: PortfolioIdDep, year: Year
) -> Response:
    """Every dividend with its ex-date in ``year``: gross, withholding, net."""
    body = _service(services).dividends_csv(portfolio_id, year)
    return _csv(body, f"dividends-{portfolio_id}-{year}.csv")


class FxRateView(BaseModel):
    base_currency: str
    quote_currency: str
    day: date
    #: Units of quote per one base, or null when no stored rate gives it.
    rate: float | None


@fx_router.get("/rate", response_model=FxRateView, operation_id="getFxRate")
def get_fx_rate(
    services: ServicesDep,
    base: Annotated[str, Query(pattern=r"^[A-Za-z]{3}$")],
    quote: Annotated[str, Query(pattern=r"^[A-Za-z]{3}$")],
    day: date | None = None,
) -> FxRateView:
    """The rate the system converts with: the latest on or before ``day``
    (default today), the inverse pair, or a cross through USD."""
    from stonks.fx import load_fx_rates

    on = day or datetime.now(UTC).date()
    b, q = base.upper(), quote.upper()
    if b == q:
        raise ValidationError("base and quote must differ")
    with services.context.lake() as lake:
        fx = load_fx_rates(lake, {b, q}, end=on)
    return FxRateView(base_currency=b, quote_currency=q, day=on, rate=fx.rate(b, q, on))
