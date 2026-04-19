"""Vendor-to-canonical key renaming. Dict-driven; pure; leaves unknown keys alone."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def rename_keys(row: Mapping[str, Any], mapping: Mapping[str, str]) -> dict[str, Any]:
    return {mapping.get(k, k): v for k, v in row.items()}
