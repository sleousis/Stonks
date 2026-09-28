"""The batched screener reads (roadmap 20.11): every metric comes from a
few set-based DuckDB queries, and gives the same values as the per-ticker
reference it replaced, on a random lake with gaps, missing values, stale
names and data that arrives after the screen date (P12)."""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any, cast

import numpy as np
import pandas as pd
import pytest

from stonks.screener import metric_ids
from stonks.screener.data import FILING_LAG_DAYS, ScreenData, finite

AS_OF = date(2024, 12, 31)
N = 40


# ---- a random lake ----------------------------------------------------------------------


def _seed(lake, rng: np.random.Generator) -> list[str]:
    names = [f"R{i:03d}.US" for i in range(N)]
    frames = []
    for i, t in enumerate(names):
        # stale names stop trading before the date, young names have few bars
        end = AS_OF - timedelta(days=int(rng.choice([0, 1, 3, 30]))) if i % 7 else AS_OF
        days = pd.bdate_range(end=end, periods=int(rng.choice([15, 30, 70, 200, 300])))
        close = 30.0 * np.exp(np.cumsum(rng.normal(0, 0.02, len(days))))
        adj = close * 0.97
        adj[rng.random(len(days)) < 0.1] = np.nan  # the vendor gave no adjusted close
        close_col = close.copy()
        close_col[rng.random(len(days)) < 0.05] = np.nan
        volume = rng.integers(1_000, 1_000_000, len(days)).astype(float)
        volume[rng.random(len(days)) < 0.05] = np.nan
        if i == 3:
            adj[-5] = -1.0  # a bad print: no volatility for this name
        frames.append(
            pd.DataFrame(
                {
                    "ticker": t,
                    "date": [d.date() for d in days],
                    "open": close,
                    "high": close,
                    "low": close,
                    "close": close_col,
                    "adj_close": adj,
                    "volume": volume,
                }
            )
        )
    lake.upsert_prices(pd.concat(frames, ignore_index=True))
    income, balance = [], []
    for i, t in enumerate(names):
        ends = pd.date_range(end=AS_OF, periods=12, freq="QE")
        for j, end in enumerate(ends):
            if i % 5 == 1 and j % 4 == 2:
                continue  # a missing quarter breaks the trailing sum
            filed = end.date() + timedelta(days=int(rng.integers(20, 80)))
            row = {
                "ticker": t,
                "period_end": end.date(),
                "frequency": "Q",
                "filing_date": None if (i + j) % 9 == 0 else filed,
                "currency": "USD",
            }
            revenue = float(rng.uniform(1e6, 1e8)) if (i + j) % 23 else None
            income.append(row | {"revenue": revenue, "net_income": float(rng.normal(1e6, 2e6))})
            balance.append(
                row
                | {
                    "total_stockholder_equity": float(rng.normal(5e7, 5e7)),
                    "short_long_term_debt_total": None
                    if j % 3 == 1
                    else float(rng.uniform(0, 1e8)),
                    "long_term_debt": float(rng.uniform(0, 1e7)) if j % 2 else None,
                    "short_term_debt": None,
                    "common_stock_shares_outstanding": float(rng.uniform(1e6, 1e8)),
                }
            )
        if i % 4 == 0:
            for year in (2021, 2022, 2023, 2024):
                income.append(
                    {
                        "ticker": t,
                        "period_end": date(year, 12, 31),
                        "frequency": "A",
                        "filing_date": date(year + 1, 3, 1),
                        "currency": "USD",
                        "revenue": float(rng.uniform(1e7, 1e9)),
                        "net_income": float(rng.normal(1e7, 1e7)),
                    }
                )
    # annual-only names: no quarters at all
    for t in names[-4:]:
        income = [r for r in income if r["ticker"] != t or r["frequency"] == "A"]
    lake.upsert_income_statement(pd.DataFrame(income))
    lake.upsert_balance_sheet(pd.DataFrame(balance))
    lake.upsert_market_cap_history(
        pd.DataFrame(
            [
                {
                    "ticker": t,
                    "date": AS_OF - timedelta(days=int(rng.integers(0, 20))),
                    "market_cap": float(rng.uniform(1e7, 1e10)),
                }
                for t in names[::3]
            ]
        )
    )
    lake.upsert_dividends(
        pd.DataFrame(
            [
                {
                    "ticker": t,
                    "ex_date": AS_OF - timedelta(days=int(rng.integers(0, 500))),
                    "amount": float(rng.uniform(0.1, 1.0)),
                    "currency": "USD",
                    "pay_date": None,
                    "record_date": None,
                    "declaration_date": None,
                }
                for t in names[::2]
                for _ in range(3)
            ]
        ).drop_duplicates(["ticker", "ex_date"])
    )
    return names


def _seed_future(lake, names: list[str]) -> None:
    """Data the screen must never see: bars after the date, statements
    filed after it, a later market cap and a later dividend."""
    later = pd.bdate_range(AS_OF + timedelta(days=1), periods=30)
    lake.upsert_prices(
        pd.DataFrame(
            [
                {
                    "ticker": t,
                    "date": d.date(),
                    "open": 1e6,
                    "high": 1e6,
                    "low": 1e6,
                    "close": 1e6,
                    "adj_close": 1e6,
                    "volume": 1e9,
                }
                for t in names
                for d in later
            ]
        )
    )
    rows = [
        {
            "ticker": t,
            "period_end": date(2025, 3, 31),
            "frequency": "Q",
            "filing_date": date(2025, 5, 1),
            "currency": "USD",
            "revenue": 9e12,
            "net_income": 9e12,
        }
        for t in names
    ]
    lake.upsert_income_statement(pd.DataFrame(rows))
    lake.upsert_balance_sheet(
        pd.DataFrame(
            [
                {k: r[k] for k in ("ticker", "period_end", "frequency", "filing_date", "currency")}
                | {"total_stockholder_equity": 1.0, "common_stock_shares_outstanding": 1.0}
                for r in rows
            ]
        )
    )
    lake.upsert_market_cap_history(
        pd.DataFrame([{"ticker": t, "date": date(2025, 1, 2), "market_cap": 1.0} for t in names])
    )
    lake.upsert_dividends(
        pd.DataFrame(
            [
                {
                    "ticker": t,
                    "ex_date": date(2025, 1, 3),
                    "amount": 99.0,
                    "currency": "USD",
                    "pay_date": None,
                    "record_date": None,
                    "declaration_date": None,
                }
                for t in names
            ]
        )
    )


@pytest.fixture
def random_lake(lake):
    return lake, _seed(lake, np.random.default_rng(20_11))


# ---- the per-ticker reference (the 20.8 implementation) ------------------------------------

_INCOME = ("revenue", "net_income")


def _ratio(top: dict[str, float], bottom: dict[str, float]) -> dict[str, float]:
    return {t: top[t] / b for t, b in bottom.items() if t in top and b > 0}


def _trailing(quarters: pd.DataFrame, skip: int) -> pd.Series | None:
    rows = quarters.iloc[skip : skip + 4]
    if len(rows) < 4:
        return None
    ends = pd.to_datetime(rows["period_end"])
    if (ends.iloc[0] - ends.iloc[-1]).days > 300:
        return None
    return rows[list(_INCOME)].astype(float).sum(skipna=False)


def _reference_income(lake, names: list[str], as_of: date) -> dict[str, dict[str, float]]:
    known = f"COALESCE(filing_date, CAST(period_end + INTERVAL {FILING_LAG_DAYS} DAY AS DATE))"
    df = lake.con.execute(
        f"""SELECT ticker, period_end, frequency, revenue, net_income FROM income_statement
             WHERE ticker = ANY(?) AND {known} <= ? ORDER BY ticker, period_end DESC""",
        [names, as_of],
    ).df()
    out: dict[str, dict[str, float]] = {}
    for ticker, rows in df.groupby("ticker"):
        q = cast(pd.DataFrame, rows.loc[rows["frequency"] == "Q"])
        a = cast(pd.DataFrame, rows.loc[rows["frequency"] == "A"])
        now, prior = _trailing(q, 0), _trailing(q, 4)
        if now is None and not a.empty:
            now = a.iloc[0][list(_INCOME)].astype(float)
            prior = a.iloc[1][list(_INCOME)].astype(float) if len(a) > 1 else None
        if now is None:
            continue
        row = {c: float(cast(Any, now[c])) for c in _INCOME}
        row["revenue_prior"] = float(cast(Any, prior["revenue"])) if prior is not None else math.nan
        out[str(ticker)] = row
    return out


def _reference(data: ScreenData, lake, names: list[str]) -> dict[str, dict[str, float]]:
    adjusted = data.adjusted
    bars = data.bars.dropna(subset=["close", "volume"])
    traded = (bars["close"] * bars["volume"]).groupby(bars["ticker"])
    income = _reference_income(lake, names, data.as_of)

    def col(name: str) -> dict[str, float]:
        return finite({t: r[name] for t, r in income.items()})

    def ret(n: int) -> dict[str, float]:
        return {
            t: float(p[-1] / p[-1 - n] - 1.0)
            for t, p in adjusted.items()
            if len(p) > n and p[-1 - n] > 0
        }

    vol: dict[str, float] = {}
    for t, p in adjusted.items():
        window = p[-64:]
        if len(window) >= 21 and not (window <= 0).any():
            vol[t] = float(np.diff(np.log(window)).std(ddof=1) * np.sqrt(252))
    equity = data.column(data.balance, "total_stockholder_equity")
    cap = data.market_cap
    growth = _ratio(col("revenue"), col("revenue_prior"))
    return {
        "price": data.last_close,
        "dollar_volume_20d": traded.apply(lambda s: s.tail(20).mean()).to_dict()
        if not bars.empty
        else {},
        "return_1m": ret(21),
        "return_3m": ret(63),
        "return_6m": ret(126),
        "return_12m": ret(252),
        "volatility_3m": vol,
        "from_high_52w": {
            t: float(p[-1] / p[-252:].max() - 1.0)
            for t, p in adjusted.items()
            if len(p) and p[-252:].max() > 0
        },
        "market_cap": cap,
        "pe_ratio": _ratio(cap, col("net_income")),
        "pb_ratio": _ratio(cap, equity),
        "ps_ratio": _ratio(cap, col("revenue")),
        "dividend_yield": _ratio(data.dividends_ttm, data.last_close),
        "net_margin": _ratio(col("net_income"), col("revenue")),
        "roe": _ratio(col("net_income"), equity),
        "debt_to_equity": _ratio(data.column(data.balance, "total_debt"), equity),
        "revenue_growth": {t: v - 1.0 for t, v in growth.items()},
    }


def _assert_same(got: dict[str, float], want: dict[str, float], metric: str) -> None:
    want = finite(want)
    assert set(got) == set(want), metric
    for t, v in want.items():
        assert got[t] == pytest.approx(v, rel=1e-9, abs=1e-12), (metric, t)


# ---- tests ----------------------------------------------------------------------------


def test_batched_metrics_match_the_per_ticker_reference(random_lake):
    lake, names = random_lake
    for as_of in (AS_OF, AS_OF - timedelta(days=40), date(2024, 3, 15)):
        data = ScreenData(lake, names, as_of)
        want = _reference(data, lake, names)
        # the chart setups (roadmap 23.13) are factor reads, tested on their own
        batched = [m for m in metric_ids() if not m.startswith("setup_")]
        assert set(want) == set(batched)
        for metric in batched:
            _assert_same(data.metric(metric), want[metric], metric)
        # the reference sees some of everything, so the comparison means something
        assert len(want["volatility_3m"]) > 5 and len(want["revenue_growth"]) > 5
        assert len(want["debt_to_equity"]) > 5


def test_batched_reads_ignore_data_known_after_the_date(random_lake):
    """P12 on the batched path: bars after the date, statements filed after
    it, a later market cap and a later dividend change no value."""
    lake, names = random_lake
    before = {m: ScreenData(lake, names, AS_OF).metric(m) for m in metric_ids()}
    _seed_future(lake, names)
    after = ScreenData(lake, names, AS_OF)
    for metric in metric_ids():
        assert after.metric(metric) == pytest.approx(before[metric]), metric
    # and the same rows do count once the date moves past them
    later = ScreenData(lake, names, AS_OF + timedelta(days=45))
    assert max(later.metric("price").values()) == pytest.approx(1e6)


def test_built_in_metrics_never_load_bars_into_pandas(random_lake):
    lake, names = random_lake
    data = ScreenData(lake, names, AS_OF)
    for metric in metric_ids():
        data.metric(metric)
    assert "bars" not in data.__dict__ and "adjusted" not in data.__dict__
