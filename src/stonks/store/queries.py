"""Small typed helpers over DuckDB queries."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import duckdb


def fetch_scalar(
    con: duckdb.DuckDBPyConnection, sql: str, params: Sequence[Any] | None = None
) -> Any:
    """The first column of the first row ``sql`` returns. Raises
    ``LookupError`` when it returns no row (an aggregate always returns
    one)."""
    row = con.execute(sql, params).fetchone() if params is not None else con.execute(sql).fetchone()
    if row is None:
        raise LookupError(f"query returned no row: {sql}")
    return row[0]
