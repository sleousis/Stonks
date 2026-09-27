"""Factor attribution of P&L (roadmap 22.4): betas, contributions that add
up to the total, grouped sectors and the report section."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from stonks.portfolio.factor_model import cross_section_returns
from stonks.reporting.factor_attribution import (
    attribute_returns,
    render_factor_attribution_section,
    returns_from_curve,
)

DATES = pd.bdate_range("2024-01-01", periods=250)


def _factors(seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    return pd.DataFrame(
        {
            "market": rng.normal(0.0004, 0.01, len(DATES)),
            "momentum": rng.normal(0.0002, 0.004, len(DATES)),
            "size": rng.normal(0.0, 0.003, len(DATES)),
            "sector:Tech": rng.normal(0.0, 0.002, len(DATES)),
            "sector:Energy": rng.normal(0.0, 0.002, len(DATES)),
        },
        index=DATES,
    )


def test_betas_are_recovered_and_parts_add_up():
    f = _factors()
    noise = np.random.default_rng(1).normal(0, 0.0002, len(DATES))
    r = pd.Series(0.0003 + 0.8 * f["market"] + 0.5 * f["momentum"] + noise, index=DATES)
    got = attribute_returns(r, f)
    assert got is not None
    assert got.betas["market"] == pytest.approx(0.8, abs=0.03)
    assert got.betas["momentum"] == pytest.approx(0.5, abs=0.05)
    assert got.factor_total + got.specific == pytest.approx(got.total, abs=1e-12)
    assert set(got.contributions) == {"market", "momentum", "size", "sector"}
    assert got.contributions["market"] == pytest.approx(0.8 * f["market"].sum(), rel=0.05)
    assert got.specific == pytest.approx(0.0003 * len(DATES), abs=0.02)
    assert got.r_squared > 0.9
    assert got.cumulative_factor[-1][1] == pytest.approx(got.factor_total)
    assert got.cumulative_specific[-1][1] == pytest.approx(got.specific)


def test_only_common_dates_count_and_short_windows_give_none():
    f = _factors()
    r = pd.Series(f["market"].to_numpy(), index=DATES)
    assert attribute_returns(r.iloc[:10], f) is None
    assert attribute_returns(r, pd.DataFrame()) is None
    got = attribute_returns(r.iloc[100:], f)
    assert got is not None and got.n_bars == len(DATES) - 100


def test_returns_from_curve():
    got = returns_from_curve([date(2024, 1, 1), date(2024, 1, 2), date(2024, 1, 3)], [100, 110, 99])
    assert got.tolist() == pytest.approx([0.1, -0.1])


def test_the_section_escapes_and_lists_every_line():
    f = _factors().rename(columns={"size": "<b>size</b>"})
    r = pd.Series(0.5 * f["market"].to_numpy(), index=DATES)
    html = render_factor_attribution_section(attribute_returns(r, f), title="<x>")
    assert "Factor attribution" in html
    assert "<b>size</b>" not in html and "&lt;b&gt;size&lt;/b&gt;" in html
    assert "specific" in html and "2 sectors" in html and "&lt;x&gt;" in html
    assert render_factor_attribution_section(None) == ""


def test_cross_section_with_few_names_gives_the_market_only():
    r = pd.Series({"A": 0.01, "B": 0.03})
    b = pd.DataFrame({"momentum": [1.0, -1.0]}, index=["A", "B"])
    assert cross_section_returns(r, b) == {"market": pytest.approx(0.02)}
    assert cross_section_returns(pd.Series(dtype=float), b) == {}
