"""Unit tests for the AssetClass type and core-level helpers.

The literal lives in ``core.types`` so every block (ingest, lake,
strategies, ranker) imports from the same place. These tests pin the closed
set so reordering or renaming the literals trips a test rather than a
runtime ValidationError on real data.
"""

from typing import get_args

from stonks.core.types import AssetClass


def test_asset_class_closed_set():
    assert set(get_args(AssetClass)) == {"equity", "crypto", "commodity", "bond"}
