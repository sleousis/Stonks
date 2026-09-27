"""Migration files are numbered 001, 002, ... with no gap (BE-63).

The runners apply every file whose number is not recorded yet, in name
order. A gap filled later would run after newer migrations on existing
installs, so a gap is refused here instead."""

from __future__ import annotations

from pathlib import Path

import pytest

import stonks.store as store

STORE = Path(store.__file__).parent


@pytest.mark.parametrize("folder", ["migrations_sqlite", "migrations_duckdb"])
def test_migration_numbers_are_contiguous_and_unique(folder):
    numbers = [int(p.stem.split("_", 1)[0]) for p in (STORE / folder).glob("*.sql")]
    assert sorted(numbers) == list(range(1, len(numbers) + 1)), folder
