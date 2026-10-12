"""A factor panel slot carries the code identity (P46): a change in the
package source misses the cache, so no panel the old code computed is
served again."""

from __future__ import annotations

from datetime import date

from stonks.factors import cache as cache_mod
from stonks.factors.cache import slot_of

ARGS = ("momentum_12_1", ["A.US", "B.US"], (date(2024, 1, 1), date(2024, 6, 1)), "1d")


def test_the_same_code_gives_the_same_slot():
    assert slot_of(*ARGS) == slot_of(*ARGS)


def test_a_code_change_moves_the_slot(monkeypatch):
    before = slot_of(*ARGS)
    monkeypatch.setattr(cache_mod, "source_hash", lambda: "another build")
    assert slot_of(*ARGS) != before


def test_a_code_change_misses_the_memory_cache(monkeypatch):
    import pandas as pd

    from stonks.factors.cache import MemoryPanelCache, PanelKey

    store = MemoryPanelCache()
    panel = pd.DataFrame({"x": [1.0]})
    store.put(PanelKey(slot_of(*ARGS), "fp"), panel)
    assert store.get(PanelKey(slot_of(*ARGS), "fp")) is not None
    monkeypatch.setattr(cache_mod, "source_hash", lambda: "another build")
    assert store.get(PanelKey(slot_of(*ARGS), "fp")) is None
