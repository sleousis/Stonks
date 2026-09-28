"""The market breadth card over HTTP (roadmap 23.14): computed from the
seeded lake, display only, any signed-in reader."""

from __future__ import annotations

from datetime import date

import pytest

from tests.integration.app.conftest import AUTH


def test_breadth_reads_the_lake(client):
    res = client.get("/api/market/breadth", headers=AUTH)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["universe"] == "every stock in the lake"
    b = body["breadth"]
    assert b["as_of"] == "2026-04-01"
    # UP.US rises, DOWN.US falls, FLAT.US holds
    assert (b["members"], b["advancers"], b["decliners"], b["unchanged"]) == (3, 1, 1, 1)
    assert b["above_50"] == {"days": 50, "count": 1, "eligible": 3, "pct": pytest.approx(1 / 3)}
    assert b["above_200"]["eligible"] == 0
    assert (b["new_highs"], b["new_lows"]) == (1, 1)
    # SPY.US is not in the seeded lake: no distribution line
    assert b["distribution_days"] is None
    assert {line["key"] for line in b["lines"]} == {
        "advance_decline",
        "above_average",
        "highs_lows",
    }


def test_breadth_on_an_earlier_day_and_an_empty_one(client, services):
    b = client.get("/api/market/breadth", params={"as_of": "2026-01-15"}, headers=AUTH).json()[
        "breadth"
    ]
    assert b["as_of"] == "2026-01-15"
    empty = services.market.breadth(as_of=date(2020, 1, 1)).breadth
    assert empty.as_of is None and empty.lines[0].key == "empty"
