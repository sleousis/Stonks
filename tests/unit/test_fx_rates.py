"""FX conversion seam (roadmap 20.5): latest rate on or before the day, the
inverse pair, a cross through USD, minor units, and no guessing."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from stonks.fx import FxRateMissing, FxRates, load_fx_rates
from stonks.store.lake import DuckDBLake

D1, D2, D3 = date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)


def rates() -> FxRates:
    return FxRates(
        [
            ("EUR", "USD", D1, 1.10),
            ("EUR", "USD", D3, 1.20),
            ("USD", "JPY", D1, 150.0),
            ("GBP", "USD", D1, 1.25),
        ]
    )


def test_same_currency_is_identity_without_rates():
    assert FxRates([]).rate("USD", "USD", D1) == 1.0
    assert FxRates([]).convert(12.5, "eur", "EUR", D1) == 12.5


def test_direct_pair_uses_latest_rate_on_or_before_the_day():
    fx = rates()
    assert fx.rate("EUR", "USD", D1) == pytest.approx(1.10)
    assert fx.rate("EUR", "USD", D2) == pytest.approx(1.10)  # carried forward
    assert fx.rate("EUR", "USD", D3) == pytest.approx(1.20)


def test_no_rate_before_the_first_observation():
    assert rates().rate("EUR", "USD", date(2025, 12, 31)) is None


def test_inverse_pair():
    assert rates().rate("USD", "EUR", D1) == pytest.approx(1 / 1.10)


def test_cross_through_usd():
    # EUR -> JPY = EUR->USD * USD->JPY
    assert rates().rate("EUR", "JPY", D1) == pytest.approx(1.10 * 150.0)
    assert rates().rate("JPY", "GBP", D1) == pytest.approx((1 / 150.0) / 1.25)


def test_minor_units_gbx_is_pence():
    fx = rates()
    assert fx.rate("GBX", "GBP", D1) == pytest.approx(0.01)
    assert fx.convert(250.0, "GBX", "USD", D1) == pytest.approx(2.5 * 1.25)
    assert fx.convert(1.0, "USD", "GBp", D1) == pytest.approx(100 / 1.25)


def test_missing_rate_returns_none_or_raises():
    fx = rates()
    assert fx.convert(10.0, "CHF", "USD", D1) is None
    with pytest.raises(FxRateMissing, match="CHF"):
        fx.convert_or_raise(10.0, "CHF", "USD", D1)


def test_load_from_lake(tmp_path):
    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()
    lake.upsert_fx_rates(
        pd.DataFrame(
            [
                {
                    "base_currency": "EUR",
                    "quote_currency": "USD",
                    "observation_date": D1,
                    "rate": 1.1,
                    "source": "fake",
                },
                {
                    "base_currency": "EUR",
                    "quote_currency": "USD",
                    "observation_date": D1,
                    "rate": 1.15,
                    "source": "fake",
                },
            ]
        )
    )
    fx = load_fx_rates(lake)
    assert fx.rate("EUR", "USD", D2) == pytest.approx(1.15)  # last write wins
    assert lake.get_fx_rates(["EUR", "USD"], end=date(2025, 1, 1)).empty
    lake.close()


def test_sum_in_base():
    from stonks.fx import sum_in_base

    fx = rates()
    total, missing = sum_in_base([(10.0, "EUR"), (5.0, None), (1.0, "USD")], "USD", fx, D1)
    assert total == pytest.approx(11.0 + 5.0 + 1.0) and missing == []
    total, missing = sum_in_base([(10.0, "CHF")], "USD", None, D1)
    assert total is None and missing == ["CHF"]


def test_values_in_base_revalues_foreign_holdings_per_day():
    from stonks.fx.valuation import CloseSeries, SnapshotPoint, values_in_base

    snaps = [
        SnapshotPoint(D1, 100.0, {"A.XETRA": 2.0}, 999.0),
        SnapshotPoint(D3, 50.0, {"A.XETRA": 1.0, "NOPRICE": 3.0}, 999.0),
    ]
    closes = CloseSeries({"A.XETRA": [(D1, 10.0)]})
    points, missing = values_in_base(snaps, closes, {"A.XETRA": "EUR"}, "USD", rates())
    assert missing == []
    assert points == [(D1, pytest.approx(100 + 2 * 10 * 1.10)), (D3, pytest.approx(50 + 10 * 1.20))]
    # a book with only base-currency holdings keeps the stored totals
    same, _ = values_in_base(snaps, closes, {}, "USD", rates())
    assert [v for _, v in same or []] == [999.0, 999.0]
    none, missing = values_in_base(snaps, closes, {"A.XETRA": "CHF"}, "USD", rates())
    assert none is None and missing == ["CHF"]
