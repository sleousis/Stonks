"""Time a screen on a synthetic lake (roadmap 20.11).

Seeds a temp lake with ``--tickers`` equities (daily bars, eight quarters of
statements, a balance sheet, market caps and dividends), then times the
screener's steps. No network.

    uv run python -m tools.screener_bench [--tickers 5000 --days 300 --repeat 3]

Steps timed (median of ``--repeat`` runs, seconds):

- ``candidates``: the universe rule on the date.
- ``metrics``: every registered metric over all candidates.
- ``run_screen``: a full screen that filters, sorts and shows every metric.
"""

from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import time
from collections.abc import Callable, Sequence
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

AS_OF = date(2026, 9, 25)
SECTORS = ("Tech", "Energy", "Health", "Finance", "Utilities")


def tickers(n: int) -> list[str]:
    return [f"S{i:05d}.US" for i in range(n)]


def seed(path: Path, n: int, days: int, *, as_of: date = AS_OF, seed: int = 11) -> list[str]:
    """A migrated lake at ``path`` with ``n`` synthetic equities."""
    from stonks.store.lake import DuckDBLake

    names = tickers(n)
    rng = np.random.default_rng(seed)
    sessions = pd.bdate_range(end=as_of, periods=days)
    lake = DuckDBLake(path)
    try:
        lake.migrate()
        steps = rng.normal(0.0003, 0.02, (n, len(sessions)))
        closes = 20.0 * np.exp(np.cumsum(steps, axis=1)) * rng.uniform(0.5, 5.0, (n, 1))
        bars = pd.DataFrame(
            {
                "ticker": np.repeat(names, len(sessions)),
                "timestamp": np.tile(sessions.to_numpy(), n),
                "interval": "1d",
                "open": closes.ravel(),
                "high": closes.ravel() * 1.01,
                "low": closes.ravel() * 0.99,
                "close": closes.ravel(),
                "adj_close": closes.ravel(),
                "volume": rng.integers(10_000, 5_000_000, n * len(sessions)),
            }
        )
        lake.con.register("seed_bars", bars)
        lake.con.execute("INSERT INTO bars SELECT * FROM seed_bars")
        lake.con.unregister("seed_bars")
        lake.con.executemany(
            "INSERT INTO instruments (id, asset_class, exchange, sector, name)"
            " VALUES (?, 'equity', 'NYSE', ?, ?)",
            [[t, SECTORS[i % len(SECTORS)], f"Name {i}"] for i, t in enumerate(names)],
        )
        quarter_ends = pd.date_range(end=as_of, periods=9, freq="QE")[:-1]
        income, balance = [], []
        for t in names:
            revenue = rng.uniform(1e6, 1e9)
            for end in quarter_ends:
                row = {
                    "ticker": t,
                    "period_end": end.date(),
                    "frequency": "Q",
                    "filing_date": (end + pd.Timedelta(days=40)).date(),
                    "currency": "USD",
                }
                income.append(
                    row | {"revenue": revenue, "net_income": revenue * rng.normal(0.08, 0.1)}
                )
            balance.append(
                row
                | {
                    "total_stockholder_equity": revenue * rng.uniform(-0.5, 4.0),
                    "short_long_term_debt_total": revenue * rng.uniform(0.0, 2.0),
                    "common_stock_shares_outstanding": rng.uniform(1e6, 1e9),
                }
            )
        lake.upsert_income_statement(pd.DataFrame(income))
        lake.upsert_balance_sheet(pd.DataFrame(balance))
        caps = pd.DataFrame(
            {"ticker": names[::2], "date": as_of, "market_cap": rng.uniform(1e7, 1e12, n // 2)}
        )
        lake.upsert_market_cap_history(caps)
        lake.upsert_dividends(
            pd.DataFrame(
                [
                    {
                        "ticker": t,
                        "ex_date": (end + pd.Timedelta(days=10)).date(),
                        "amount": 0.25,
                        "currency": "USD",
                        "pay_date": None,
                        "record_date": None,
                        "declaration_date": None,
                    }
                    for t in names[::3]
                    for end in quarter_ends[-4:]
                ]
            )
        )
        lake.con.execute("CHECKPOINT")
    finally:
        lake.close()
    return names


def _median(fn: Callable[[], Any], repeat: int) -> float:
    times = []
    for _ in range(repeat):
        start = time.perf_counter()
        fn()
        times.append(time.perf_counter() - start)
    return statistics.median(times)


def measure(path: Path, *, repeat: int = 3, as_of: date = AS_OF) -> dict[str, Any]:
    """Seconds per screener step on the lake at ``path``."""
    from stonks.screener import ScreenSpec, metric_ids, run_screen
    from stonks.screener.data import ScreenData
    from stonks.screener.engine import candidates
    from stonks.screener.spec import MAX_FILTERS
    from stonks.store.lake import DuckDBLake

    spec = ScreenSpec(
        min_price=1.0,
        filters=[{"metric": "return_12m", "min": -0.9}, {"metric": "pe_ratio", "max": 500}],
        sort_by="market_cap",
        # a screen names at most MAX_FILTERS columns; every_metric reads all
        columns=metric_ids()[:MAX_FILTERS],
    )
    lake = DuckDBLake(path, read_only=True)
    try:
        names = candidates(lake, spec, as_of)
        out: dict[str, Any] = {"candidates_count": len(names)}
        out["candidates"] = _median(lambda: candidates(lake, spec, as_of), repeat)

        def every_metric() -> None:
            data = ScreenData(lake, names, as_of)
            for m in metric_ids():
                data.metric(m)

        out["metrics"] = _median(every_metric, repeat)
        out["run_screen"] = _median(lambda: run_screen(lake, spec, as_of), repeat)
        out["matched"] = run_screen(lake, spec, as_of).matched
    finally:
        lake.close()
    return {k: round(v, 3) if isinstance(v, float) else v for k, v in out.items()}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tickers", type=int, default=5000)
    parser.add_argument("--days", type=int, default=300)
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "lake.duckdb"
        start = time.perf_counter()
        seed(path, args.tickers, args.days)
        seeded = time.perf_counter() - start
        result = {"tickers": args.tickers, "days": args.days, "seed_seconds": round(seeded, 1)}
        result |= measure(path, repeat=args.repeat)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
