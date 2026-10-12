"""``fetch_scalar``: the first column of the one row a query returns."""

from __future__ import annotations

import duckdb
import pytest

from stonks.store.queries import fetch_scalar


def test_fetch_scalar_returns_first_column() -> None:
    con = duckdb.connect()
    try:
        con.execute("CREATE TABLE t (x INTEGER)")
        con.execute("INSERT INTO t VALUES (1), (2), (3)")
        assert fetch_scalar(con, "SELECT COUNT(*) FROM t") == 3
        assert fetch_scalar(con, "SELECT MAX(x) FROM t WHERE x < ?", [3]) == 2
    finally:
        con.close()


def test_fetch_scalar_keeps_null() -> None:
    con = duckdb.connect()
    try:
        assert fetch_scalar(con, "SELECT NULL") is None
    finally:
        con.close()


def test_fetch_scalar_raises_when_no_row() -> None:
    con = duckdb.connect()
    try:
        con.execute("CREATE TABLE t (x INTEGER)")
        with pytest.raises(LookupError):
            fetch_scalar(con, "SELECT x FROM t")
    finally:
        con.close()
