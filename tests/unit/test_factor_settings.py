"""``[factors]`` settings: which panel cache the service uses."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from stonks.factors.cache import MemoryPanelCache, ParquetPanelCache
from stonks.factors.settings import FactorSettings, make_panel_cache


def test_default_is_a_parquet_cache_next_to_the_lake(tmp_path):
    cache = make_panel_cache(FactorSettings(), tmp_path / "lake.duckdb")
    assert isinstance(cache, ParquetPanelCache)
    assert cache.root == tmp_path / "factor_panels"


def test_memory_off_and_explicit_folder(tmp_path):
    assert make_panel_cache(FactorSettings(cache="off"), Path("x")) is None
    memory = make_panel_cache(FactorSettings(cache="memory", memory_panels=3), Path("x"))
    assert isinstance(memory, MemoryPanelCache) and memory.max_panels == 3
    parquet = make_panel_cache(FactorSettings(cache_dir=tmp_path / "p"), Path("x"))
    assert isinstance(parquet, ParquetPanelCache) and parquet.root == tmp_path / "p"
    with pytest.raises(ValidationError):
        FactorSettings(cache="disk")  # type: ignore[arg-type]
