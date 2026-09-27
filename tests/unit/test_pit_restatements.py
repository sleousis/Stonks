"""The fundamentals strategies are blind to a restatement Stonks saw after
the decision (P12, DuckDB 018).

Two lakes share the same past. The planted one also holds a restatement of
every past quarter that keeps the original filing dates, first seen two
days after the decision. A decision on that day must answer the same on
both lakes. A decision after Stonks saw the restatement must see it."""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from stonks.core.interval import Interval
from stonks.lab.catalog import strategy_catalog
from stonks.store.pit import PitSession
from stonks.strategies._common import decision_interval
from tests.unit.test_pit_catalog import LATER, TICKERS, D, _factory, _lake

FUNDAMENTALS = ["quality_value", "quant_value"]
SEEN = D + timedelta(days=2)


def _seen_when_filed(lake) -> None:
    """The original filings were first seen on their filing day."""
    for table in ("income_statement", "balance_sheet"):
        lake.con.execute(f"UPDATE {table}_versions SET known_at = CAST(filing_date AS TIMESTAMP)")


def _restate(lake) -> None:
    for statement, write in (
        ("income_statement", lake.upsert_income_statement),
        ("balance_sheet", lake.upsert_balance_sheet),
    ):
        frames = []
        for ticker in TICKERS[:3]:
            rows = lake.get_statement_history(statement, ticker).drop(columns=["available_date"])
            frames.append(rows)
        restated = pd.concat(frames, ignore_index=True)
        for column in ("revenue", "gross_profit"):
            if column in restated:
                restated[column] = restated[column] * 40.0
        for column in ("net_income", "ebit", "operating_income"):
            if column in restated:
                restated[column] = -restated[column] * 30.0
        if "total_stockholder_equity" in restated:
            restated["total_stockholder_equity"] = 5.0
            restated["total_assets"] = 50_000.0
        write(restated, known_at=datetime.combine(SEEN.date(), datetime.min.time()))


@pytest.fixture(scope="module")
def lakes():
    past, planted = _lake(False), _lake(False)
    for lake in (past, planted):
        _seen_when_filed(lake)
    _restate(planted)
    yield past, planted
    past.close()
    planted.close()


def test_the_restatement_overwrote_the_current_rows(lakes):
    past, planted = lakes
    now = planted.get_statement_history("income_statement", "A.US")
    before = past.get_statement_history("income_statement", "A.US")
    assert list(now["filing_date"]) == list(before["filing_date"])
    assert list(now["revenue"]) != list(before["revenue"])
    assert len(planted.get_statement_versions("income_statement", "A.US")) == 2 * len(before)


def _answers(name, lake, decision, asked=None):
    """Each ticker's score and features on a view of ``lake`` at ``decision``
    (``asked``: the day the strategy is asked about, by mistake later)."""
    view = PitSession(lake).at(decision, decision_interval=Interval.DAY_1)
    strategy = _factory(strategy_catalog()[name])()
    when = asked or decision
    with decision_interval(Interval.DAY_1):
        return {
            t: (
                strategy.estimate_return(t, when, view),
                dict(strategy.extract_features(t, when, view).values),
            )
            for t in TICKERS[:3]
        }


@pytest.mark.parametrize("name", FUNDAMENTALS)
def test_a_restatement_seen_after_the_decision_is_invisible_then(name, lakes):
    past, planted = lakes
    answers = _answers(name, planted, D)
    assert all(features for _, features in answers.values()), answers
    assert answers == _answers(name, past, D)


@pytest.mark.parametrize("name", FUNDAMENTALS)
def test_asking_about_a_later_day_by_mistake_still_hides_it(name, lakes):
    past, planted = lakes
    assert _answers(name, planted, D, LATER) == _answers(name, past, D, LATER)


@pytest.mark.parametrize("name", FUNDAMENTALS)
def test_a_decision_after_stonks_saw_it_reads_the_restatement(name, lakes):
    past, planted = lakes
    later = D + timedelta(days=3)
    assert _answers(name, planted, later) != _answers(name, past, later)
