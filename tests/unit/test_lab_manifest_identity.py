"""The manifest's data and code identity (BL-06, P12, P46): the seeds every
survival test uses, the recipe of the search, the lake tables a strategy
reads, all bar history, and a code identity without ``.git``."""

from __future__ import annotations

import subprocess
from datetime import date

import pandas as pd
import pytest

from stonks.code_identity import source_hash
from stonks.lab import manifest as manifest_mod
from stonks.lab.dataset import LabDataset
from stonks.lab.manifest import (
    build_manifest,
    build_recipe,
    changed_tables,
    code_identity_changed,
    collect_seeds,
    data_fingerprint,
    git_info,
    library_versions,
    refingerprint,
    strategy_data_tables,
)
from stonks.store.lake import DuckDBLake

# ---- seeds and recipe -------------------------------------------------------------


def _registry_tests() -> list:
    from stonks.lab.survival.registry import build_survival_test, survival_test_names

    return [build_survival_test(name, None) for name in survival_test_names()]


def _declared_seed(test) -> object:
    """The seed a test runs with, wherever it keeps it: a ``seed`` field of
    any options or config model it holds, or a seed attribute."""
    for value in vars(test).values():
        fields = getattr(type(value), "model_fields", None)
        if fields and "seed" in fields and value.seed is not None:
            return value.seed
    for name in ("seed", "_seed"):
        if getattr(test, name, None) is not None:
            return getattr(test, name)
    return None


def test_every_seeded_registry_test_is_in_the_seeds():
    tests = _registry_tests()
    seeds = collect_seeds(None, tests)["tests"]
    expected = {t.id: _declared_seed(t) for t in tests if _declared_seed(t) is not None}
    assert expected, "no registry test declares a seed"
    for test_id, seed in expected.items():
        assert seeds.get(test_id) == seed, test_id
    # the ones the options models hold (missed before)
    assert {"cpcv", "event_study", "stress", "vs_random", "walk_forward"} <= set(seeds)


def test_the_recipe_records_the_search():
    from stonks.lab.heatmap import HeatmapOptions
    from stonks.lab.objectives import SharpeObjective
    from stonks.lab.survival.vs_random import VsRandomTest
    from stonks.lab.tuning.grid import GridTuner

    recipe = build_recipe(
        GridTuner(grid_size=5, seed=3),
        SharpeObjective(),
        [VsRandomTest()],
        budget=7,
        heatmap=HeatmapOptions(x="a", y="b", grid_size=4),
        fixed_params={"skip_days": 0},
    )
    assert recipe["tuner"] == {
        "class": "stonks.lab.tuning.grid:GridTuner",
        "seed": 3,
        "options": {"grid_size": 5},
    }
    assert recipe["objective"]["name"] == "sharpe"
    (vs_random,) = recipe["suite"]
    assert vs_random["id"] == "vs_random" and vs_random["options"]["seed"] == 17
    assert recipe["budget"] == 7
    assert recipe["heatmap"]["x"] == "a" and recipe["heatmap"]["grid_size"] == 4
    assert recipe["fixed_params"] == {"skip_days": 0}


def test_the_manifest_carries_the_recipe_and_the_backtest_block():
    ds = LabDataset(lake=None, universe=["X.US"], start=date(2024, 1, 1), end=date(2024, 6, 1))
    m = build_manifest(None, ds, {}, recipe={"budget": 3})
    assert m["recipe"] == {"budget": 3}
    assert m["backtest"] == {
        "execution": None,
        "construction": None,
        "benchmark": "auto",
        "embargo_bars": 0,
    }


# ---- data tables ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "table"),
    [
        ("stonks.strategies.macro_regime:MacroRegimeFilter", "macro_indicators"),
        ("stonks.strategies.examples.tvl_deviation:TVLDeviationStrategy", "defi_tvl"),
        ("stonks.strategies.examples.earnings_drift:EarningsDrift", "earnings_calendar"),
        ("stonks.strategies.examples.filing_events:FilingEvents", "corporate_filings"),
        ("stonks.strategies.examples.quality_value:QualityValue", "shares_outstanding"),
    ],
)
def test_strategies_declare_the_tables_they_read(path, table):
    from stonks.strategies._wrapping import import_strategy_class

    assert table in strategy_data_tables(import_strategy_class(path))


def test_a_wrapper_resolves_its_inner_strategy_tables():
    from stonks.strategies.trailing_stop import TrailingStopWrapper

    inner = "stonks.strategies.examples.tvl_deviation:TVLDeviationStrategy"
    assert strategy_data_tables(TrailingStopWrapper, {"inner_class_path": inner}) == ("defi_tvl",)
    from stonks.strategies.macro_regime import MacroRegimeFilter

    both = strategy_data_tables(MacroRegimeFilter, {"inner_class_path": inner})
    assert both == ("defi_tvl", "macro_indicators")


def test_a_plain_strategy_reads_no_extra_table():
    from stonks.strategies.examples.momentum import Momentum

    assert strategy_data_tables(Momentum) == ()


# ---- data fingerprint -------------------------------------------------------------


@pytest.fixture
def lake():
    lake = DuckDBLake(":memory:")
    lake.migrate()
    days = pd.bdate_range("2021-01-01", "2025-06-30")
    lake.upsert_prices(
        pd.DataFrame(
            {
                "ticker": "A.US",
                "date": [d.date() for d in days],
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "adj_close": 1.0,
                "volume": 1,
            }
        )
    )
    yield lake
    lake.close()


def _ds(lake) -> LabDataset:
    return LabDataset(lake=lake, universe=["A.US"], start=date(2025, 1, 1), end=date(2025, 6, 30))


def _macro(lake, value: float) -> None:
    lake.upsert_macro_indicators(
        pd.DataFrame(
            [
                {
                    "country_iso": "USA",
                    "indicator": "gdp_growth_annual",
                    "observation_date": date(2025, 3, 1),
                    "period": "annual",
                    "country_name": "US",
                    "value": value,
                }
            ]
        )
    )


def test_a_macro_revision_changes_the_hash_and_names_the_table(lake):
    _macro(lake, 2.0)
    before = data_fingerprint(_ds(lake), ["macro_indicators", "defi_tvl"])
    assert set(before["tables"]) == {"macro_indicators", "defi_tvl"}
    assert data_fingerprint(_ds(lake), ["macro_indicators", "defi_tvl"]) == before
    _macro(lake, -5.0)
    after = refingerprint(lake, before)
    assert after["hash"] != before["hash"]
    assert changed_tables(before, after) == ["macro_indicators"]


def test_a_tvl_day_changes_the_hash_and_names_the_table(lake):
    before = data_fingerprint(_ds(lake), ["defi_tvl"])
    lake.upsert_defi_tvl(
        pd.DataFrame(
            [
                {
                    "chain": "Ethereum",
                    "observation_date": date(2025, 3, 1),
                    "tvl_usd": 1e9,
                    "source": "x",
                }
            ]
        )
    )
    after = refingerprint(lake, before)
    assert after["hash"] != before["hash"]
    assert changed_tables(before, after) == ["defi_tvl"]


def test_table_rows_after_the_window_end_are_left_out(lake):
    before = data_fingerprint(_ds(lake), ["macro_indicators"])
    lake.upsert_macro_indicators(
        pd.DataFrame(
            [
                {
                    "country_iso": "USA",
                    "indicator": "cpi_yoy",
                    "observation_date": date(2025, 9, 1),
                    "period": "monthly",
                    "country_name": "US",
                    "value": 3.0,
                }
            ]
        )
    )
    assert refingerprint(lake, before)["hash"] == before["hash"]


def test_without_declared_tables_the_fingerprint_has_no_tables_part(lake):
    assert "tables" not in data_fingerprint(_ds(lake))


def test_a_bar_three_years_before_the_start_changes_the_hash(lake):
    before = data_fingerprint(_ds(lake))
    assert before["warmup_days"] is None
    lake.con.execute(
        "UPDATE bars SET close = 2.0 WHERE ticker = 'A.US' AND timestamp = TIMESTAMP '2022-01-03'"
    )
    after = refingerprint(lake, before)
    assert after["hash"] != before["hash"]
    assert data_fingerprint(_ds(lake))["hash"] == after["hash"]


def test_an_old_fingerprint_keeps_its_warm_up(lake):
    """A fingerprint stored with the 550-day warm-up is recomputed with it,
    so an old run does not report every bar before it as changed."""
    old = manifest_mod._fingerprint(
        lake, ["A.US"], [], "1d", date(2025, 1, 1), date(2025, 6, 30), statements=True
    )
    assert old["warmup_days"] == 550
    lake.con.execute(
        "UPDATE bars SET close = 2.0 WHERE ticker = 'A.US' AND timestamp = TIMESTAMP '2022-01-03'"
    )
    assert refingerprint(lake, old)["hash"] == old["hash"]


def test_the_manifest_fingerprint_covers_the_strategy_tables(lake):
    from stonks.strategies.examples.tvl_deviation import TVLDeviationStrategy

    m = build_manifest(None, _ds(lake), {}, strategy=TVLDeviationStrategy)
    assert set(m["data_fingerprint"]["tables"]) == {"defi_tvl"}


# ---- code identity ----------------------------------------------------------------


def _no_git(monkeypatch) -> None:
    def missing(*a, **k):
        raise FileNotFoundError("git")

    monkeypatch.setattr(manifest_mod.subprocess, "run", missing)


def test_git_info_falls_back_to_the_build_sha(monkeypatch):
    _no_git(monkeypatch)
    monkeypatch.setenv("STONKS_GIT_SHA", "abc123")
    assert git_info() == {"git_sha": "abc123", "git_dirty": None}


def test_a_failed_git_falls_back_to_the_build_sha(monkeypatch):
    monkeypatch.setattr(
        manifest_mod.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 128, stdout="", stderr="not a repo"),
    )
    monkeypatch.setenv("STONKS_GIT_SHA", "def456")
    assert git_info()["git_sha"] == "def456"


def test_without_git_the_manifest_has_a_source_hash_and_verify_uses_it(monkeypatch):
    _no_git(monkeypatch)
    monkeypatch.delenv("STONKS_GIT_SHA", raising=False)
    m = build_manifest(None, None, {})
    assert m["git_sha"] is None
    assert m["source_hash"] == source_hash() and len(m["source_hash"]) == 64
    assert code_identity_changed(m) is False
    assert code_identity_changed({**m, "source_hash": "0" * 64}) is True
    assert code_identity_changed({"git_sha": None}) is None


def test_the_git_sha_wins_when_both_sides_have_one(monkeypatch):
    monkeypatch.setattr(manifest_mod, "git_info", lambda: {"git_sha": "a", "git_dirty": False})
    assert code_identity_changed({"git_sha": "a", "source_hash": "x"}) is False
    assert code_identity_changed({"git_sha": "b", "source_hash": source_hash()}) is True


def test_source_hash_tracks_every_python_file(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_bytes(b"x = 1\n")
    (tmp_path / "b.py").write_bytes(b"y = 2\n")
    base = source_hash(tmp_path)
    (tmp_path / "pkg" / "a.py").write_bytes(b"x = 1\r\n")
    assert source_hash(tmp_path) == base  # line endings do not matter
    (tmp_path / "pkg" / "a.py").write_bytes(b"x = 3\n")
    assert source_hash(tmp_path) != base
    (tmp_path / "notes.txt").write_text("not code")
    (tmp_path / "pkg" / "a.py").write_bytes(b"x = 1\n")
    assert source_hash(tmp_path) == base


def test_the_libraries_cover_the_numeric_stack_and_the_lock_file():
    libs = library_versions()
    for name in ("optuna", "cvxpy", "statsmodels", "arch", "exchange-calendars", "QuantLib"):
        assert name in libs
    assert "uv.lock" in libs
    if libs["uv.lock"] is not None:
        assert len(libs["uv.lock"]) == 64
