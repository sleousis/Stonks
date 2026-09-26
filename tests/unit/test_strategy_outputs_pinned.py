"""Pin the bar-reading strategies' outputs on fixed synthetic data.

Guards refactors of how strategies read bars (e.g. the shared bar cache):
the signals must stay bit-for-bit identical. The golden values in
``tests/fixtures/strategies/pinned_outputs.json`` were produced by the
per-call query implementation. Regenerate only for an intended change:

    uv run python tests/unit/test_strategy_outputs_pinned.py
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from stonks.core.interval import Interval
from stonks.lab.dataset import LabDataset
from stonks.store.lake import DuckDBLake
from stonks.strategies.examples.donchian_breakout import DonchianBreakout
from stonks.strategies.examples.momentum import Momentum
from stonks.strategies.examples.rsi_pca import RSIPCAStrategy
from stonks.strategies.examples.trendline_breakout import TrendlineBreakoutStrategy

GOLDEN = Path(__file__).parent.parent / "fixtures" / "strategies" / "pinned_outputs.json"
DAILY_TICKERS = ("AAA.US", "BBB.US")
HOURLY_TICKER = "HHH.US"


def _build_lake(path: Path) -> DuckDBLake:
    lake = DuckDBLake(path)
    lake.migrate()
    rng = np.random.default_rng(20260926)
    days = pd.bdate_range("2024-01-02", periods=420)
    rows = []
    for i, ticker in enumerate(DAILY_TICKERS):
        closes = 50.0 * (i + 1) * np.exp(np.cumsum(rng.normal(0.0004, 0.018, len(days))))
        for d, c in zip(days, closes, strict=True):
            rows.append(
                {
                    "ticker": ticker,
                    "date": d.date(),
                    "open": c,
                    "high": c * 1.01,
                    "low": c * 0.99,
                    "close": c,
                    "adj_close": c,
                    "volume": 1_000_000,
                }
            )
    lake.upsert_prices(pd.DataFrame(rows))

    hours = [
        datetime(d.year, d.month, d.day, h)
        for d in pd.bdate_range("2025-01-06", periods=40)
        for h in range(14, 21)
    ]
    closes = 100.0 + np.cumsum(rng.normal(0.0, 0.6, len(hours)))
    lake.upsert_bars(
        pd.DataFrame(
            {
                "ticker": HOURLY_TICKER,
                "timestamp": hours,
                "open": closes,
                "high": closes + 0.2,
                "low": closes - 0.2,
                "close": closes,
                "adj_close": closes,
                "volume": 1_000,
            }
        ),
        Interval.HOUR_1,
    )
    return lake


def _daily_as_ofs() -> list[date | datetime]:
    out: list[date | datetime] = [date(2023, 12, 1), date(2024, 1, 10)]
    out += [d.date() for d in pd.date_range("2024-02-01", "2025-08-20", freq="11D")]
    # weekend, datetime mid-day, datetime at midnight
    out += [date(2025, 3, 15), datetime(2025, 3, 14, 13, 30), datetime(2025, 3, 14)]
    return out


def _hourly_as_ofs() -> list[datetime]:
    base = [datetime(2025, 1, 6, 9), datetime(2025, 1, 13, 16, 30)]
    base += [
        datetime(d.year, d.month, d.day, h)
        for d in pd.bdate_range("2025-01-20", "2025-02-28", freq="3B")
        for h in (14, 17, 20)
    ]
    base += [datetime(2025, 2, 8, 12), datetime(2025, 2, 10, 13, 59)]
    return base


def _enc(x: float | None) -> float | None | str:
    if x is None:
        return None
    if isinstance(x, float) and math.isnan(x):
        return "nan"
    return float(x)


def _features(strategy, ticker, as_of, lake) -> dict[str, float | None | str]:
    return {
        k: _enc(v) for k, v in sorted(strategy.extract_features(ticker, as_of, lake).values.items())
    }


def compute_outputs(lake: DuckDBLake) -> dict[str, list]:
    out: dict[str, list] = {}

    momentum = Momentum({"lookback_days": 30, "threshold": -1.0})
    out["momentum"] = [
        [str(a), t, _enc(momentum.estimate_return(t, a, lake))]
        for a in _daily_as_ofs()
        for t in DAILY_TICKERS
    ]

    cases = {
        "donchian_1d": (DonchianBreakout, {"lookback": 20, "ticker": "AAA.US"}, _daily_as_ofs()),
        "donchian_1h": (
            DonchianBreakout,
            {"lookback": 15, "ticker": HOURLY_TICKER, "interval": "1h"},
            _hourly_as_ofs(),
        ),
        "trendline_1d": (
            TrendlineBreakoutStrategy,
            {"lookback": 30, "ticker": "BBB.US"},
            _daily_as_ofs(),
        ),
        "trendline_1h": (
            TrendlineBreakoutStrategy,
            {"lookback": 20, "ticker": HOURLY_TICKER, "interval": "1h"},
            _hourly_as_ofs(),
        ),
    }
    for name, (cls, params, as_ofs) in cases.items():
        s = cls(params)
        ticker = params["ticker"]
        out[name] = [
            [str(a), _enc(s.estimate_return(ticker, a, lake)), _features(s, ticker, a, lake)]
            for a in as_ofs
        ]

    rsi = RSIPCAStrategy({"ticker": "AAA.US", "long_quantile": 0.9})
    rsi.fit(
        LabDataset(lake=lake, universe=["AAA.US"], start=date(2024, 1, 2), end=date(2025, 8, 29))
    )
    out["rsi_pca"] = [
        [str(a), _enc(rsi.estimate_return("AAA.US", a, lake)), _features(rsi, "AAA.US", a, lake)]
        for a in _daily_as_ofs()
    ]
    return out


@pytest.fixture(scope="module")
def pinned_lake(tmp_path_factory):
    lake = _build_lake(tmp_path_factory.mktemp("pinned") / "lake.duckdb")
    yield lake
    lake.close()


def test_strategy_outputs_match_pinned_golden_values(pinned_lake):
    golden = json.loads(GOLDEN.read_text())
    actual = json.loads(json.dumps(compute_outputs(pinned_lake)))
    assert actual.keys() == golden.keys()
    for name in golden:
        assert _close(actual[name], golden[name]), name


def _close(a, b) -> bool:
    """Structural equality with a tiny float tolerance: the last bits of
    floating-point results differ across OSes and Python builds."""
    if isinstance(a, float) or isinstance(b, float):
        return (
            isinstance(a, int | float)
            and isinstance(b, int | float)
            and math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12)
        )
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_close(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_close(a[k], b[k]) for k in a)
    return a == b


def test_pinned_outputs_are_not_trivial():
    """The golden file must exercise real signals, not just ``None``s."""
    golden = json.loads(GOLDEN.read_text())
    for name, rows in golden.items():
        estimates = [r[2] if name == "momentum" else r[1] for r in rows]
        assert any(e is not None for e in estimates), name
        assert any(e is None for e in estimates), name


if __name__ == "__main__":  # pragma: no cover - golden regeneration
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        lake = _build_lake(Path(tmp) / "lake.duckdb")
        try:
            GOLDEN.parent.mkdir(parents=True, exist_ok=True)
            GOLDEN.write_text(json.dumps(compute_outputs(lake), indent=1, sort_keys=True))
        finally:
            lake.close()
    print(f"wrote {GOLDEN}")
