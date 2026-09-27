"""The capacity tools (roadmap 14.10): the light API load and the benchmark."""

from __future__ import annotations

import os
import urllib.error

import pytest

from tools import api_load, benchmark


def test_percentile_interpolates():
    assert api_load.percentile([], 0.5) == 0.0
    assert api_load.percentile([10.0], 0.99) == 10.0
    assert api_load.percentile([1.0, 2.0, 3.0, 4.0], 0.5) == pytest.approx(2.5)
    assert api_load.percentile([4.0, 1.0, 3.0, 2.0], 1.0) == 4.0


def test_summarize_counts_errors_as_requests():
    result = api_load.summarize([10.0, 20.0, 30.0], errors=1, seconds=2.0)
    assert result.requests == 4
    assert result.errors == 1
    assert result.requests_per_second == 2.0
    assert result.p50_ms == 20.0
    assert result.max_ms == 30.0


def test_run_load_cycles_paths_across_clients():
    seen: list[str] = []

    def fetch(path: str) -> None:
        seen.append(path)
        if path == "/boom":
            raise urllib.error.URLError("down")

    result = api_load.run_load(
        "http://unused",
        clients=2,
        seconds=0.05,
        think_seconds=0.0,
        paths=["/a", "/boom"],
        fetch=fetch,
    )
    assert {"/a", "/boom"} <= set(seen)
    assert result.errors == seen.count("/boom")
    assert result.requests == len(seen)


def test_api_load_main_prints_a_summary(monkeypatch, capsys):
    def fake_run_load(*args, **kwargs):
        return api_load.summarize([5.0], errors=0, seconds=1.0)

    monkeypatch.setattr(api_load, "run_load", fake_run_load)
    assert api_load.main(["--seconds", "0.1"]) == 0
    assert "p95" in capsys.readouterr().out
    assert api_load.main(["--json"]) == 0
    assert '"p50_ms": 5.0' in capsys.readouterr().out


def test_seed_lake_and_sizes(tmp_path):
    path = tmp_path / "lake.duckdb"
    benchmark.seed_lake(path, benchmark.tickers(3), 20)
    assert benchmark.lake_bytes(path) > 0
    assert benchmark.dir_bytes(tmp_path) >= benchmark.lake_bytes(path)
    assert benchmark.dir_bytes(tmp_path / "missing") == 0


def test_storage_section_reports_bytes_per_ticker_year(tmp_path):
    out = benchmark.bench_storage(n_tickers=4, years=1, keep=tmp_path)
    assert out["rows"] == 4 * benchmark.TRADING_DAYS
    assert out["duckdb_bytes_per_ticker_year"] > 0
    assert out["parquet_bytes_per_ticker_year"] > 0


def test_tick_section_times_a_small_book():
    out = benchmark.bench_tick_once(2, 2, n_tickers=5, ticks=1)
    assert out["strategies"] == 2
    assert out["portfolios"] == 2
    assert out["tick_seconds"] > 0


def test_rss_of_this_process_is_known():
    rss = benchmark.rss_bytes(os.getpid())
    assert rss is None or rss > 1024 * 1024


def test_the_parser_names_every_section():
    args = benchmark.build_parser().parse_args(["tick", "--strategies", "1,2"])
    assert args.section == "tick"
    assert benchmark._ints(args.strategies) == [1, 2]
    assert benchmark._mb(None) is None
    assert benchmark._mb(3 * benchmark.MB) == 3.0
