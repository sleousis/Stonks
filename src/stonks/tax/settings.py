"""Estimated tax rates per jurisdiction (roadmap 23.5), ``[tax.rates.*]``.

A rough guide for the pre-trade preview and the running tax owed this
year, never tax advice. Set the rates that apply to you: a short-term and a
long-term rate for each jurisdiction (``us``, ``eu``, ``uk``). The EU and
UK make no short and long split, so both rates default to the same value.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from stonks.tax.lots import Jurisdiction


class TaxRates(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Rate on gains held one year or less (and every short sale).
    short_term: float = Field(ge=0.0, le=1.0)
    #: Rate on gains held more than one year.
    long_term: float = Field(ge=0.0, le=1.0)


class TaxRatesConfig(BaseModel):
    """``[tax.rates]``: one rate pair per jurisdiction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    us: TaxRates = TaxRates(short_term=0.32, long_term=0.15)
    eu: TaxRates = TaxRates(short_term=0.26, long_term=0.26)
    uk: TaxRates = TaxRates(short_term=0.24, long_term=0.24)

    def for_jurisdiction(self, jurisdiction: Jurisdiction) -> TaxRates:
        rates: TaxRates = getattr(self, jurisdiction)
        return rates


class TaxConfig(BaseModel):
    """``[tax]``: settings of the tax block."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rates: TaxRatesConfig = TaxRatesConfig()
