"""Forecast weights report section (roadmap 22.7): each rule's weight, cost
and whether the speed limit dropped it."""

from __future__ import annotations

from dataclasses import replace

from stonks.features.forecast_weights import ForecastWeightFit, RuleFit
from stonks.reporting.forecast_weights import (
    render_forecast_weights_section,
    strategy_report_sections,
)
from stonks.reporting.tearsheet import TearSheet, render_tear_sheet
from tests.unit.test_reporting_tearsheet import _report

EVIL = "<script>x</script>"


def _fit(**kw) -> ForecastWeightFit:
    base = ForecastWeightFit(
        method="handcraft",
        status="ok",
        n_obs=500,
        fdm=1.31,
        sigma_annual=0.2,
        cost=0.00025,
        max_cost_sr=0.13,
        rules=(
            RuleFit("ewmac2", 10.6, 0.0, 98.0, 0.21, 0.4, 0.19, True, "speed limit: costs 0.210"),
            RuleFit("ewmac64", 1.87, 1.0, 5.0, 0.01, 0.3, 0.29, False),
        ),
        fit_end="2022-12-30",
    )
    return replace(base, **kw)


def test_section_shows_each_rule_weight_cost_and_drop():
    html = render_forecast_weights_section({"AAPL.US": _fit()})
    assert "Forecast weights" in html
    assert "AAPL.US" in html and "handcraft" in html
    assert "ewmac2" in html and "ewmac64" in html
    assert "100.0%" in html  # ewmac64's weight
    assert "0.210" in html  # ewmac2's cost in SR a year
    assert "dropped" in html and "speed limit" in html
    assert "1.31" in html  # the FDM


def test_section_escapes_values():
    html = render_forecast_weights_section({EVIL: _fit(method=EVIL)})
    assert EVIL not in html


def test_no_fits_no_section():
    assert render_forecast_weights_section({}) == ""


class _Blend:
    def forecast_weight_fits(self):
        return {"X.US": _fit()}


class _Wrapper:
    def __init__(self, inner):
        self.inner = inner


def test_sections_found_on_the_strategy_or_inside_a_wrapper():
    assert len(strategy_report_sections(_Blend())) == 1
    assert len(strategy_report_sections(_Wrapper(_Blend()))) == 1
    assert strategy_report_sections(object()) == []


def test_tear_sheet_renders_extra_sections():
    section = render_forecast_weights_section({"X.US": _fit()})
    html = render_tear_sheet(TearSheet(title="t", report=_report(), sections=(section,)))
    assert "Forecast weights" in html
    assert render_tear_sheet(TearSheet(title="t", report=_report())).count("Forecast") == 0
