"""Lab runs on a universe id: resolve members over the window (delisted
names included) and, opt in, ensure their data before the preflight."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.ingest.ensure import DataEnsurer, EnsureSettings
from stonks.lab.dataset import LabDataset
from stonks.lab.runner import LabRunner
from stonks.lab.survival.base import SurvivalSuite
from stonks.lab.universe_data import prepare_dataset
from stonks.strategies.examples.buy_and_hold import BuyAndHold
from stonks.universes import UniverseDefinition, UniverseStore, refresh_universe
from tests.fixtures.universes import FakeListingSource, bars
from tests.unit.test_lab_runner_preflight import _Objective, _Tuner


@pytest.fixture
def lake(tmp_path):
    from stonks.store.lake import DuckDBLake

    lk = DuckDBLake(tmp_path / "lake.duckdb")
    lk.migrate()
    UniverseStore(lk).save(
        UniverseDefinition(
            id="mine",
            kind="list",
            spec={
                "tickers": ["A.US"],
                # DEAD left (was delisted) in the middle of the window
                "spans": [
                    {"ticker": "DEAD.US", "start_date": "2010-01-01", "end_date": "2024-03-01"}
                ],
            },
        )
    )
    refresh_universe(lk, "mine", as_of=date(2025, 1, 1))
    yield lk
    lk.close()


def _ds(lake, **kw) -> LabDataset:
    return LabDataset(
        lake=lake,
        universe_id="mine",
        start=date(2024, 1, 1),
        end=date(2024, 6, 28),
        benchmark="none",
        **kw,
    )


def _ensurer(lake, source) -> DataEnsurer:
    return DataEnsurer(lake, source, EnsureSettings(max_workers=2), today=date(2025, 1, 1))


def test_prepare_resolves_members_over_the_window(lake):
    dataset, report = prepare_dataset(_ds(lake))
    assert dataset.universe == ["A.US", "DEAD.US"]
    assert report is None


def test_prepare_keeps_an_explicit_universe(lake):
    dataset, _ = prepare_dataset(_ds(lake, universe=["A.US"]))
    assert dataset.universe == ["A.US"]


def test_prepare_ensures_data_including_warmup(lake):
    source = FakeListingSource(
        prices={
            "A.US": bars("A.US", date(2023, 1, 2), date(2024, 6, 28)),
            "DEAD.US": bars("DEAD.US", date(2023, 1, 2), date(2024, 2, 29)),
        }
    )
    dataset, report = prepare_dataset(
        _ds(lake), ensurer=_ensurer(lake, source), strategy=BuyAndHold
    )
    assert report is not None and report.tickers_fetched == 2
    assert sorted(lake.sql("SELECT DISTINCT ticker FROM bars").ticker) == ["A.US", "DEAD.US"]


def test_prepare_ensures_reference_tickers_too(lake):
    source = FakeListingSource(
        prices={
            "A.US": bars("A.US", date(2023, 1, 2), date(2024, 6, 28)),
            "DEAD.US": bars("DEAD.US", date(2023, 1, 2), date(2024, 2, 29)),
            "REF.US": bars("REF.US", date(2023, 1, 2), date(2024, 6, 28)),
        }
    )
    _, report = prepare_dataset(
        _ds(lake, reference_tickers=("REF.US",)),
        ensurer=_ensurer(lake, source),
        strategy=BuyAndHold,
    )
    assert report is not None and report.tickers_fetched == 3
    assert "REF.US" in set(lake.sql("SELECT DISTINCT ticker FROM bars").ticker)


def test_lab_runner_ensures_before_the_preflight(lake):
    source = FakeListingSource(
        prices={
            "A.US": bars("A.US", date(2023, 6, 1), date(2024, 6, 28)),
            "DEAD.US": bars("DEAD.US", date(2023, 6, 1), date(2024, 2, 29)),
        }
    )
    tuner = _Tuner()
    runner = LabRunner(
        tuner=tuner,
        objective=_Objective(),
        suite=SurvivalSuite([]),
        budget=1,
        data_ensurer=_ensurer(lake, source),
    )
    result = runner.run(BuyAndHold, _ds(lake))
    assert tuner.calls == 1
    codes = {i.code for i in result.preflight.issues}
    # the named universe is point in time and every member got its data
    assert "static_universe" not in codes and "membership_gap" not in codes
    assert "no_data" not in codes and "missing_data" not in codes
    assert result.manifest["ensure"]["tickers_fetched"] == 2
    assert result.manifest["dataset"]["universe_id"] == "mine"


def test_lab_runner_without_an_ensurer_still_resolves_the_universe(lake):
    tuner = _Tuner()
    runner = LabRunner(tuner=tuner, objective=_Objective(), suite=SurvivalSuite([]), budget=1)
    # no bars: the preflight stops the run, but on the resolved members
    with pytest.raises(Exception, match="no_data"):
        runner.run(BuyAndHold, _ds(lake))
