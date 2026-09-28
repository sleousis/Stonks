"""Currency and tax reads (roadmap 20.5): a portfolio's base currency and
tax settings, its specific-lot picks, and the FX rate the system converts
with. Changing settings and downloading the CSV exports are done in the
console or the CLI."""

# No ``from __future__ import annotations`` (see common.py).

from datetime import date
from typing import Annotated, Any, Literal

from pydantic import Field

from stonks.mcp.tools.common import READ, PortfolioId, ToolContext, drop_none, items

Currency = Annotated[str, Field(pattern=r"^[A-Za-z]{3}$", description="ISO code, e.g. EUR")]


def register(t: ToolContext) -> None:
    server = t.server

    @server.tool(annotations=READ)
    async def get_tax_settings(portfolio_id: PortfolioId = None) -> dict[str, Any]:
        """One of your portfolios' base currency and tax settings: the
        jurisdiction (us, eu or uk), the lot method (fifo or specific) and
        whether US wash sales are adjusted."""
        return await t.get("/api/tax/settings", drop_none({"portfolio_id": portfolio_id}))

    @server.tool(annotations=READ)
    async def list_tax_lot_picks(
        portfolio_id: PortfolioId = None, sell_fill_id: int | None = None
    ) -> dict[str, Any]:
        """The specific lots each sell closes (used with lot method specific)."""
        query = drop_none({"portfolio_id": portfolio_id, "sell_fill_id": sell_fill_id})
        return items(await t.get("/api/tax/lots/picks", query))

    @server.tool(annotations=READ)
    async def preview_trade_tax(
        ticker: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._\-^=]{0,31}$")],
        side: Literal["buy", "sell"],
        quantity: Annotated[float, Field(gt=0, le=1e9)],
        price: Annotated[float | None, Field(gt=0, description="Default: latest close")] = None,
        portfolio_id: PortfolioId = None,
    ) -> dict[str, Any]:
        """Before a trade in one of your portfolios: the lots a sell closes
        under your lot method, the realised gain and holding period, the
        estimated tax at the configured rate, the after-tax proceeds and a
        US wash sale warning. An estimate, not tax advice. Places nothing."""
        query = drop_none(
            {
                "portfolio_id": portfolio_id,
                "ticker": ticker,
                "side": side,
                "quantity": quantity,
                "price": price,
            }
        )
        return await t.get("/api/tax/preview", query)

    @server.tool(annotations=READ)
    async def get_tax_year(
        year: Annotated[int | None, Field(ge=1900, le=2200)] = None,
        portfolio_id: PortfolioId = None,
    ) -> dict[str, Any]:
        """The gains one of your portfolios realised this year (or ``year``)
        and the estimated tax owed on them, in its base currency."""
        return await t.get("/api/tax/year", drop_none({"portfolio_id": portfolio_id, "year": year}))

    @server.tool(annotations=READ)
    async def get_fx_rate(
        base: Currency,
        quote: Currency,
        day: Annotated[date | None, Field(description="YYYY-MM-DD; default today")] = None,
    ) -> dict[str, Any]:
        """The FX rate the system converts with: units of quote per one base,
        the latest stored on or before the day (inverse pair or a cross
        through USD when needed). Null when no stored rate gives it."""
        query = drop_none({"base": base, "quote": quote, "day": day.isoformat() if day else None})
        return await t.get("/api/fx/rate", query)
