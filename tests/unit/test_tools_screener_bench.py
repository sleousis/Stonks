"""The screener benchmark (roadmap 20.11) runs end to end on a tiny lake."""

from __future__ import annotations

import pytest

from tools import screener_bench


@pytest.mark.slow
def test_screener_bench_seeds_and_times_a_small_lake(tmp_path):
    path = tmp_path / "lake.duckdb"
    names = screener_bench.seed(path, 30, 280)
    assert len(names) == 30
    out = screener_bench.measure(path, repeat=1)
    assert out["candidates_count"] == 30
    assert 0 < out["matched"] <= 30
    for step in ("candidates", "metrics", "run_screen"):
        assert out[step] >= 0
