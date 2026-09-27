"""The tear sheet's factor attribution follows a stored universe's
point-in-time membership (roadmap 22.10)."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pandas as pd

import stonks.app.tearsheets as tearsheets

SPANS = pd.DataFrame(
    [
        {"universe_id": "u", "ticker": "A.US", "start_date": date(2020, 1, 1), "end_date": None},
        {
            "universe_id": "u",
            "ticker": "B.US",
            "start_date": date(2024, 3, 1),
            "end_date": date(2024, 6, 1),
        },
    ]
)


class _Lake:
    def members_between(self, universe_id, start, end):
        assert universe_id == "u"
        return ["A.US", "B.US"]

    def get_universe_membership(self, universe_id=None, tickers=None):
        assert universe_id == "u"
        return SPANS


def _report():
    return SimpleNamespace(equity_dates=[date(2024, 1, 2)], equity_curve=[1.0])


def test_a_stored_universe_passes_its_membership(monkeypatch):
    seen = {}

    def fake(lake, universe, start, end, **kw):
        seen.update(universe=list(universe), **kw)
        return pd.DataFrame()

    monkeypatch.setattr(tearsheets, "style_factor_returns", fake)
    request = SimpleNamespace(
        universe=[], universe_id="u", start=date(2024, 1, 1), end=date(2024, 12, 31)
    )
    assert tearsheets._factor_attribution(_Lake(), request, _report(), "t") == ""
    assert seen["universe"] == ["A.US", "B.US"] and seen["universe_id"] == "u"
    assert list(seen["membership"].columns) == ["ticker", "start_date", "end_date"]
    assert seen["membership"]["ticker"].tolist() == ["A.US", "B.US"]


def test_a_ticker_list_has_no_membership(monkeypatch):
    seen = {}

    def fake(lake, universe, start, end, **kw):
        seen.update(kw)
        return pd.DataFrame()

    monkeypatch.setattr(tearsheets, "style_factor_returns", fake)
    request = SimpleNamespace(
        universe=["A.US"], universe_id=None, start=date(2024, 1, 1), end=date(2024, 12, 31)
    )
    tearsheets._factor_attribution(_Lake(), request, _report(), "t")
    assert seen.get("membership") is None
