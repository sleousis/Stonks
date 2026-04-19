"""Integration tests for LabDataset — the view over the lake that defines
train/val windows and the universe any lab component sees.
"""

from __future__ import annotations

from datetime import date

from stonks.lab.dataset import LabDataset


def test_train_and_val_windows_partition_the_full_range(lake_trending):
    ds = LabDataset(
        lake=lake_trending,
        universe=["UP.US", "DOWN.US"],
        start=date(2025, 10, 1),
        end=date(2026, 4, 1),
        train_ratio=0.5,
    )
    t_start, t_end = ds.train_window
    v_start, v_end = ds.val_window
    assert t_start == ds.start
    assert v_end == ds.end
    assert v_start > t_end  # no overlap
    # train_ratio=0.5 means roughly equal halves
    train_days = (t_end - t_start).days
    total_days = (ds.end - ds.start).days
    assert 0.4 <= train_days / total_days <= 0.6


def test_universe_honored(lake_trending):
    ds = LabDataset(
        lake=lake_trending,
        universe=["UP.US"],
        start=date(2025, 10, 1),
        end=date(2026, 4, 1),
    )
    prices = ds.prices_on(date(2026, 1, 15))
    assert set(prices) == {"UP.US"}


def test_prices_on_returns_empty_for_non_trading_day_in_universe(lake_trending):
    ds = LabDataset(
        lake=lake_trending,
        universe=["UP.US", "FLAT.US"],
        start=date(2025, 10, 1),
        end=date(2026, 4, 1),
    )
    prices = ds.prices_on(date(1999, 1, 1))
    assert prices == {}
