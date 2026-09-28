"""Screens at scale (roadmap 20.11): the candidate cap, progress for the
background job, and the short result cache per (spec, as-of date)."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.config import Settings
from stonks.screener import ScreenSpec, run_screen
from stonks.screener.cache import ScreenCache
from stonks.screener.engine import TooManyCandidates, candidates
from stonks.screener.settings import ScreenerSettings
from tests.fixtures.screener import END, seed_market


@pytest.fixture
def market(lake):
    return seed_market(lake)


# ---- the candidate cap -------------------------------------------------------------------


def test_the_cap_stops_a_screen_before_any_metric_is_read(market):
    spec = ScreenSpec(filters=[{"metric": "pe_ratio", "max": 100}])
    with pytest.raises(TooManyCandidates) as err:
        run_screen(market, spec, END, max_candidates=2)
    assert err.value.count == 3 and err.value.cap == 2
    message = str(err.value)
    assert "3 candidates" in message and "cap of 2" in message
    assert "sectors" in message and "universe_id" in message
    # at the cap is fine
    assert run_screen(market, spec, END, max_candidates=3).candidates == 3
    assert isinstance(err.value, ValueError)


def test_candidates_counts_what_the_rule_keeps(market):
    assert candidates(market, ScreenSpec(sectors=["Tech"]), END) == ["AAA.US", "BBB.US"]


# ---- progress ---------------------------------------------------------------------------------


def test_progress_rises_to_one_with_a_step_per_metric(market):
    seen: list[tuple[float, str]] = []
    spec = ScreenSpec(
        filters=[{"metric": "return_12m", "min": -1}], sort_by="price", columns=["pe_ratio"]
    )
    result = run_screen(market, spec, END, progress=lambda f, m: seen.append((f, m)))
    fractions = [f for f, _ in seen]
    assert fractions == sorted(fractions) and fractions[-1] == 1.0
    assert 0 < fractions[0] < 1
    messages = " ".join(m for _, m in seen)
    for metric in ("return_12m", "price", "pe_ratio"):
        assert metric in messages
    assert "3 candidates" in seen[0][1]
    assert f"{result.matched} matched" in seen[-1][1]


def test_a_progress_callback_can_stop_the_screen(market):
    class Stop(Exception):
        pass

    def stop(fraction: float, message: str) -> None:
        if fraction > 0.1:
            raise Stop

    with pytest.raises(Stop):
        run_screen(market, ScreenSpec(columns=["price", "pe_ratio"]), END, progress=stop)


# ---- the cache -----------------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_cache_keeps_a_result_per_spec_and_date_for_a_short_time(market):
    clock = _Clock()
    cache = ScreenCache(ttl_seconds=60, max_entries=8, clock=clock)
    spec = ScreenSpec(sort_by="price", limit=2)
    result = run_screen(market, spec, END)
    assert cache.get(spec, END) is None
    cache.put(spec, END, result)
    assert cache.get(spec, END) == result
    # the same spec written another way is the same key
    same = ScreenSpec.model_validate({"limit": 2, "sort_by": "price", "descending": True})
    assert cache.get(same, END) == result
    # another date or another spec misses
    assert cache.get(spec, date(2024, 12, 30)) is None
    assert cache.get(ScreenSpec(sort_by="price", limit=1), END) is None
    clock.now += 61
    assert cache.get(spec, END) is None
    assert len(cache) == 0


def test_cache_drops_the_oldest_entry_when_full(market):
    cache = ScreenCache(ttl_seconds=60, max_entries=2, clock=_Clock())
    result = run_screen(market, ScreenSpec(), END)
    specs = [ScreenSpec(limit=n) for n in (1, 2, 3)]
    cache.put(specs[0], END, result)
    cache.put(specs[1], END, result)
    assert cache.get(specs[0], END) is not None  # a hit keeps it fresh
    cache.put(specs[2], END, result)
    assert cache.get(specs[1], END) is None
    assert cache.get(specs[0], END) is not None and cache.get(specs[2], END) is not None


def test_a_zero_ttl_turns_the_cache_off(market):
    cache = ScreenCache(ttl_seconds=0, max_entries=8)
    spec = ScreenSpec()
    cache.put(spec, END, run_screen(market, spec, END))
    assert cache.get(spec, END) is None


# ---- settings ------------------------------------------------------------------------------


def test_screener_settings_defaults_and_config_section():
    s = ScreenerSettings()
    assert s.max_candidates >= 5000 and s.job_threshold < s.max_candidates
    assert s.cache_seconds > 0
    assert Settings().screener.max_candidates == s.max_candidates
    with pytest.raises(ValueError):
        ScreenerSettings(max_candidates=0)
    with pytest.raises(ValueError):
        ScreenerSettings(typo=1)
