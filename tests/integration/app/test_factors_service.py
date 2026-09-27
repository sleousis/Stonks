"""FactorService (roadmap 22.2, 22.3, 22.8): the catalog, formula checks,
values at a date and tear sheets, over a real lake."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from stonks.app.context import AppContext
from stonks.app.errors import NotFoundError, ValidationError
from stonks.app.factors import (
    ExpressionCheckRequest,
    FactorService,
    FactorTearSheetRequest,
    FactorValuesRequest,
)
from stonks.factors.cache import ParquetPanelCache
from stonks.store.lake import DuckDBLake

TICKERS = [f"FAC{i:02d}.US" for i in range(12)]


def seed_factor_lake(path) -> None:
    rng = np.random.default_rng(9)
    dates = pd.bdate_range(start="2025-06-02", end="2026-04-01")
    rows = []
    for i, ticker in enumerate(TICKERS):
        closes = 50.0 * np.exp(np.cumsum(rng.normal(0.0002 * i, 0.02, len(dates))))
        for d, c in zip(dates, closes, strict=True):
            rows.append(
                {"ticker": ticker, "date": d.date(), "open": c * 0.998, "high": c * 1.01,
                 "low": c * 0.99, "close": c, "adj_close": c, "volume": 1_000_000}
            )  # fmt: skip
    lake = DuckDBLake(path)
    try:
        lake.migrate()
        lake.upsert_prices(pd.DataFrame(rows))
        lake.upsert_universe_membership(
            pd.DataFrame(
                [
                    {
                        "universe_id": "fac",
                        "ticker": t,
                        "start_date": date(2025, 1, 1),
                        "end_date": None if i < 11 else date(2026, 1, 2),
                    }
                    for i, t in enumerate(TICKERS)
                ]
            )
        )
    finally:
        lake.close()


@pytest.fixture
def service(settings) -> FactorService:
    seed_factor_lake(settings.lake.path)
    return FactorService(AppContext(settings))


def test_catalog_and_filters(service):
    full = service.catalog()
    names = {s.name: s.count for s in full.sets}
    assert names["alpha158"] == 157
    assert len(full.factors) == sum(names.values())
    alpha = service.catalog(set="alpha158")
    assert len(alpha.factors) == 157
    assert all(f.set == "alpha158" for f in alpha.factors)
    value = service.catalog(family="value")
    assert {f.id for f in value.factors} >= {"ebit_tev", "book_to_market"}
    assert all(f.kind == "fundamental" for f in service.catalog(kind="fundamental").factors)
    with pytest.raises(NotFoundError):
        service.catalog(set="nope")


def test_get_one_factor(service):
    view = service.get("mom_12_1")
    assert view.family == "momentum" and view.set == "classic"
    assert view.lookback_bars == 252
    with pytest.raises(NotFoundError):
        service.get("nope")


def test_check_expression(service):
    ok = service.check_expression(ExpressionCheckRequest(expression="Mean($close, 5) / $close"))
    assert ok.ok and ok.canonical == "Div(Mean($close,5),$close)" and ok.lookback_bars == 4
    bad = service.check_expression(ExpressionCheckRequest(expression="Ref($close, -1)"))
    assert not bad.ok and "future" in bad.error
    mixed = service.check_expression(ExpressionCheckRequest(expression="$close > 10"))
    assert not mixed.ok


def test_values_rank_in_the_factor_direction(service):
    view = service.values(
        FactorValuesRequest(factor="low_vol_60", universe=TICKERS, as_of=date(2026, 3, 2))
    )
    assert view.direction == -1
    values = [v.value for v in view.values]
    assert values == sorted(values)  # calmest first
    assert [v.rank for v in view.values] == list(range(1, len(values) + 1))
    by_universe = service.values(
        FactorValuesRequest(factor="KMID", universe_id="fac", as_of=date(2026, 3, 2))
    )
    assert len(by_universe.values) == 11  # FAC11 left the universe on 2026-01-02
    with pytest.raises(ValidationError):
        service.values(
            FactorValuesRequest(factor="$close > 1", universe=TICKERS, as_of=date(2026, 3, 2))
        )
    with pytest.raises(NotFoundError):
        service.values(
            FactorValuesRequest(factor="KMID", universe_id="nope", as_of=date(2026, 3, 2))
        )


def test_request_needs_exactly_one_universe():
    with pytest.raises(ValueError, match="exactly one"):
        FactorValuesRequest(factor="KMID", as_of=date(2026, 3, 2))
    with pytest.raises(ValueError, match="exactly one"):
        FactorValuesRequest(factor="KMID", universe=["A"], universe_id="x", as_of=date(2026, 3, 2))
    with pytest.raises(ValueError, match="before"):
        FactorTearSheetRequest(
            factor="KMID", universe=["A"], start=date(2026, 1, 2), end=date(2026, 1, 1)
        )


def test_tearsheet_through_the_panel_cache(service):
    assert isinstance(service.cache, ParquetPanelCache)
    request = FactorTearSheetRequest(
        factor="ROC5", universe=TICKERS, start=date(2025, 9, 1), end=date(2026, 3, 31),
        horizons=[1, 5], every_bars=5,
    )  # fmt: skip
    view = service.run_tearsheet(request)
    assert view.status == "ok"
    assert view.factor.id == "ROC5" and view.factor.set == "alpha158"
    assert [h.horizon for h in view.horizons] == [1, 5]
    assert set(view.ic_by_group) == {"sector", "asset_class", "size"}
    assert view.monthly_ic and len(view.monthly_ic[0].months) == 12
    misses = service.cache.misses
    again = service.run_tearsheet(request)
    assert service.cache.hits >= 1 and service.cache.misses == misses
    assert again.horizons == view.horizons


def test_tearsheet_over_a_stored_universe(service):
    request = FactorTearSheetRequest(
        factor="mom_6_1", universe_id="fac", start=date(2025, 9, 1), end=date(2026, 3, 31),
    )  # fmt: skip
    view = service.run_tearsheet(request)
    assert view.universe_id == "fac"
    assert view.n_tickers == 12
    with pytest.raises(NotFoundError):
        service.run_tearsheet(request.model_copy(update={"universe_id": "nope"}))
