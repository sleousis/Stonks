"""Factor expressions compiled to DuckDB SQL (roadmap 22.2): hand-computed
rolling and cross-sectional values, adjustment as of each date, membership,
and a planted-future test for look-ahead (P12)."""

from __future__ import annotations

import math
from datetime import date

import numpy as np
import pandas as pd
import pytest

from stonks.core.corporate_actions import CorporateActions, Split
from stonks.core.interval import Interval
from stonks.factors.engine import (
    PanelRequest,
    evaluate,
    membership_frame,
    panel_from_lake,
    prepare_bars,
    warmup_days,
)
from stonks.factors.expression import parse, parse_factor

DATES = pd.bdate_range("2024-01-01", periods=6)


def _bars(closes: dict[str, list[float]], volumes: dict[str, list[float]] | None = None):
    rows = []
    for ticker, values in closes.items():
        vols = (volumes or {}).get(ticker, [100.0] * len(values))
        for d, c, v in zip(DATES, values, vols, strict=False):
            rows.append(
                {
                    "ticker": ticker,
                    "timestamp": d,
                    "open": c - 0.5,
                    "high": c + 1.0,
                    "low": c - 1.0,
                    "close": c,
                    "adj_close": c,
                    "volume": v,
                }
            )
    return pd.DataFrame(rows)


RAW = _bars({"A": [1, 2, 3, 4, 5, 6], "B": [6, 5, 4, 3, 2, 1]})


def _panel(text: str, raw: pd.DataFrame = RAW, **kw) -> pd.DataFrame:
    tickers = list(dict.fromkeys(raw["ticker"]))
    return evaluate(parse(text), prepare_bars(raw, **kw), tickers)


def _col(panel: pd.DataFrame, ticker: str) -> list[float]:
    return [None if math.isnan(v) else round(v, 10) for v in panel[ticker]]


N = None


@pytest.mark.parametrize(
    ("text", "a", "b"),
    [
        ("$close", [1, 2, 3, 4, 5, 6], [6, 5, 4, 3, 2, 1]),
        ("Ref($close, 1)", [N, 1, 2, 3, 4, 5], [N, 6, 5, 4, 3, 2]),
        ("Ref($close, 0)", [1, 2, 3, 4, 5, 6], [6, 5, 4, 3, 2, 1]),
        ("Mean($close, 3)", [N, N, 2, 3, 4, 5], [N, N, 5, 4, 3, 2]),
        ("Sum($close, 2)", [N, 3, 5, 7, 9, 11], [N, 11, 9, 7, 5, 3]),
        ("Std($close, 3)", [N, N, 1, 1, 1, 1], [N, N, 1, 1, 1, 1]),
        ("Min($close, 3)", [N, N, 1, 2, 3, 4], [N, N, 4, 3, 2, 1]),
        ("Max($close, 3)", [N, N, 3, 4, 5, 6], [N, N, 6, 5, 4, 3]),
        ("Delta($close, 2)", [N, N, 2, 2, 2, 2], [N, N, -2, -2, -2, -2]),
        ("Quantile($close, 3, 0.5)", [N, N, 2, 3, 4, 5], [N, N, 5, 4, 3, 2]),
        # today's value is the window's top (A) or bottom (B) of 3
        ("Rank($close, 3)", [N, N, 1, 1, 1, 1], [N, N, 1 / 3, 1 / 3, 1 / 3, 1 / 3]),
        ("IdxMax($close, 3)", [N, N, 3, 3, 3, 3], [N, N, 1, 1, 1, 1]),
        ("IdxMin($close, 3)", [N, N, 1, 1, 1, 1], [N, N, 3, 3, 3, 3]),
        ("Slope($close, 3)", [N, N, 1, 1, 1, 1], [N, N, -1, -1, -1, -1]),
        ("Rsquare($close, 3)", [N, N, 1, 1, 1, 1], [N, N, 1, 1, 1, 1]),
        ("Resi($close, 3)", [N, N, 0, 0, 0, 0], [N, N, 0, 0, 0, 0]),
        ("CSRank($close)", [0.5, 0.5, 0.5, 1, 1, 1], [1, 1, 1, 0.5, 0.5, 0.5]),
        (
            "$close / Ref($close, 1) - 1",
            [N, 1, 0.5, 1 / 3, 0.25, 0.2],
            [N, -1 / 6, -0.2, -0.25, -1 / 3, -0.5],
        ),
        ("$close > 3.5", [0, 0, 0, 1, 1, 1], [1, 1, 1, 0, 0, 0]),
        ("If($close >= 3, $close, -$close)", [-1, -2, 3, 4, 5, 6], [6, 5, 4, 3, -2, -1]),
        ("Greater($close, 3)", [3, 3, 3, 4, 5, 6], [6, 5, 4, 3, 3, 3]),
        ("Less($close, 3)", [1, 2, 3, 3, 3, 3], [3, 3, 3, 3, 2, 1]),
        ("Abs($close - 3)", [2, 1, 0, 1, 2, 3], [3, 2, 1, 0, 1, 2]),
        ("Sign($close - 3)", [-1, -1, 0, 1, 1, 1], [1, 1, 1, 0, -1, -1]),
        ("($close > 2) & ($close < 5)", [0, 0, 1, 1, 0, 0], [0, 0, 1, 1, 0, 0]),
        ("($close < 2) | ($close > 5)", [1, 0, 0, 0, 0, 1], [1, 0, 0, 0, 0, 1]),
        ("1 / ($close - 3)", [-0.5, -1, N, 1, 0.5, 1 / 3], [1 / 3, 0.5, 1, N, -1, -0.5]),
        (
            "Log($close - 3)",
            [N, N, N, 0, math.log(2), math.log(3)],
            [math.log(3), math.log(2), 0, N, N, N],
        ),
        ("Mean(Ref($close, 1), 2)", [N, N, 1.5, 2.5, 3.5, 4.5], [N, N, 5.5, 4.5, 3.5, 2.5]),
        ("CSRank(Mean($close, 2))", [N, 0.5, 0.5, 0.75, 1, 1], [N, 1, 1, 0.75, 0.5, 0.5]),
    ],
)
def test_operators_match_hand_computed_values(text, a, b):
    panel = _panel(text)
    assert list(panel.index) == list(DATES)
    assert _col(panel, "A") == [None if v is None else round(v, 10) for v in a]
    assert _col(panel, "B") == [None if v is None else round(v, 10) for v in b]


def test_corr_of_proportional_series_is_one():
    raw = _bars({"A": [1, 2, 4, 3, 5, 6]}, {"A": [2, 4, 8, 6, 10, 12]})
    assert _col(_panel("Corr($close, $volume, 3)", raw), "A") == [N, N, 1, 1, 1, 1]
    # a flat window has no correlation: empty, not NaN or infinite
    flat = _bars({"A": [1, 2, 3, 3, 3, 3]}, {"A": [1, 1, 1, 1, 1, 1]})
    assert _col(_panel("Corr($close, $volume, 3)", flat), "A") == [N] * 6


def test_rank_averages_ties_like_percentileofscore():
    raw = _bars({"A": [2, 1, 2, 2, 2, 2]})
    # window [2, 1, 2]: below 1, up to 3 -> (1 + 3 + 1) / 6
    assert _col(_panel("Rank($close, 3)", raw), "A")[2] == round(5 / 6, 10)


def test_csrank_averages_ties_like_pandas():
    raw = _bars({"A": [1] * 6, "B": [2] * 6, "C": [2] * 6, "D": [3] * 6})
    panel = _panel("CSRank($close)", raw)
    expected = pd.Series([1, 2, 2, 3], index=list("ABCD")).rank(pct=True)
    assert panel.iloc[0].tolist() == pytest.approx(expected.tolist())


def test_a_gap_inside_a_window_leaves_it_empty():
    raw = RAW.copy()
    raw.loc[(raw["ticker"] == "A") & (raw["timestamp"] == DATES[2]), "close"] = np.nan
    assert _col(_panel("Mean($close, 2)", raw), "A") == [N, 1.5, N, N, 4.5, 5.5]


def test_rolling_windows_count_rows_per_ticker():
    raw = RAW[~((RAW["ticker"] == "A") & (RAW["timestamp"] == DATES[1]))]
    # A's rows are days 0, 2, 3, 4, 5: its previous row on day 2 is day 0
    assert _col(_panel("Ref($close, 1)", raw), "A") == [N, N, 1, 3, 4, 5]


# ---- adjustment as of each date --------------------------------------------------------


def _split_bars():
    # a 2:1 split effective on day 3: raw closes halve and volumes double
    return _bars({"S": [100, 100, 100, 50, 50, 50]}, {"S": [10, 10, 10, 20, 20, 20]})


SPLIT = CorporateActions.from_events([Split("S", DATES[3].date(), 2.0)])


def test_returns_see_no_fake_crash_across_a_split():
    panel = _panel("$close / Ref($close, 1) - 1", _split_bars(), actions=SPLIT)
    assert _col(panel, "S") == [N, 0, 0, 0, 0, 0]


def test_levels_come_out_in_the_units_known_on_each_date():
    raw = _split_bars()
    assert _col(_panel("$close", raw, actions=SPLIT), "S") == [100, 100, 100, 50, 50, 50]
    assert _col(_panel("Mean($close, 2)", raw, actions=SPLIT), "S") == [N, 100, 100, 50, 50, 50]
    assert _col(_panel("Mean($volume, 2)", raw, actions=SPLIT), "S") == [N, 10, 10, 20, 20, 20]
    assert _col(_panel("Log($close)", raw, actions=SPLIT), "S")[1] == round(math.log(100), 10)
    assert _col(_panel("Log($close)", raw, actions=SPLIT), "S")[4] == round(math.log(50), 10)


def test_adj_close_fallback_adjusts_prices():
    raw = _split_bars()
    raw["adj_close"] = [50, 50, 50, 50, 50, 50]  # vendor back-adjusted closes
    panel = _panel("$close / Ref($close, 1) - 1", raw)
    assert _col(panel, "S") == [N, 0, 0, 0, 0, 0]
    assert _col(_panel("$close", raw), "S") == [100, 100, 100, 50, 50, 50]


# ---- membership ----------------------------------------------------------------------


def test_csrank_ranks_members_only_and_output_skips_non_members():
    raw = _bars({"A": [1] * 6, "B": [2] * 6, "C": [3] * 6})
    spans = membership_frame(
        {
            "A": [(DATES[0].date(), None)],
            "B": [(DATES[0].date(), None)],
            "C": [(DATES[3].date(), None)],
        }
    )
    panel = _panel("CSRank($close)", raw, membership=spans)
    assert panel.loc[DATES[0]].tolist()[:2] == [0.5, 1.0]
    assert math.isnan(panel.loc[DATES[0], "C"])
    assert panel.loc[DATES[3]].tolist() == pytest.approx([1 / 3, 2 / 3, 1.0])
    # history before joining still feeds rolling windows
    mean = _panel("Mean($close, 3)", raw, membership=spans)
    assert mean.loc[DATES[3], "C"] == 3


def test_membership_end_date_is_exclusive():
    """A member from start_date up to the day before end_date (docs/universes.md)."""
    raw = _bars({"A": [1] * 6, "B": [2] * 6})
    spans = membership_frame(
        {"A": [(DATES[0].date(), DATES[2].date())], "B": [(DATES[0].date(), None)]}
    )
    panel = _panel("$close", raw, membership=spans)
    assert panel.loc[DATES[1], "A"] == 1
    assert math.isnan(panel.loc[DATES[2], "A"])


# ---- no look-ahead (planted future) -------------------------------------------------------

EXPRESSIONS = [
    "$close / Ref($close, 5) - 1",
    "Mean($close, 5)",
    "Std($volume, 4) / Mean($volume, 4)",
    "Rank($close, 6)",
    "CSRank($close)",
    "CSRank(Mean($close, 3) / $close)",
    "Corr($close, Log($volume), 5)",
    "Slope($close, 5) / $close",
    "Resi($close, 5) / $close",
    "Quantile($close, 5, 0.8) / $close",
    "IdxMax($high, 5) - IdxMin($low, 5)",
    "Sum(Greater($close - Ref($close, 1), 0), 5)",
    "Delta(Log($close), 3)",
    "Max($high, 4) / Min($low, 4)",
    "If($close > Ref($close, 1), 1, 0)",
]


def _random_bars(n_days: int, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2024-01-01", periods=n_days)
    rows = []
    for t in ("A", "B", "C", "D"):
        closes = 50 * np.exp(np.cumsum(rng.normal(0, 0.02, n_days)))
        for d, c in zip(dates, closes, strict=True):
            rows.append(
                {
                    "ticker": t,
                    "timestamp": d,
                    "open": c * 0.99,
                    "high": c * 1.01,
                    "low": c * 0.98,
                    "close": c,
                    "adj_close": c,
                    "volume": float(rng.integers(1000, 5000)),
                }
            )
    return pd.DataFrame(rows)


@pytest.mark.parametrize("text", EXPRESSIONS)
def test_planted_future_changes_no_earlier_value(text):
    raw = _random_bars(40, seed=11)
    cut = pd.bdate_range("2024-01-01", periods=40)[25]
    # plant the future: a crash, a volume spike and a split after the cut
    planted = raw.copy()
    later = planted["timestamp"] > cut
    planted.loc[later & (planted["ticker"] == "A"), ["open", "high", "low", "close"]] *= 0.3
    planted.loc[later, "volume"] *= 50
    actions = CorporateActions.from_events(
        [Split("B", pd.bdate_range("2024-01-01", periods=40)[30].date(), 4.0)]
    )
    planted.loc[
        (planted["timestamp"] >= pd.bdate_range("2024-01-01", periods=40)[30])
        & (planted["ticker"] == "B"),
        ["open", "high", "low", "close"],
    ] /= 4.0
    node = parse_factor(text)
    tickers = ["A", "B", "C", "D"]
    honest = evaluate(node, prepare_bars(raw[raw["timestamp"] <= cut]), tickers)
    full = evaluate(node, prepare_bars(planted, actions), tickers)
    before = full.loc[full.index <= cut]
    pd.testing.assert_frame_equal(before, honest, check_freq=False, atol=1e-9, rtol=1e-9)


def test_the_label_reads_the_future_but_stops_at_the_data_end():
    from stonks.factors.expression import next_open_label

    raw = RAW
    panel = evaluate(next_open_label(1), prepare_bars(raw), ["A"])
    opens = [0.5, 1.5, 2.5, 3.5, 4.5, 5.5]
    assert _col(panel, "A") == [
        round(opens[2] / opens[1] - 1, 10),
        round(opens[3] / opens[2] - 1, 10),
        round(opens[4] / opens[3] - 1, 10),
        round(opens[5] / opens[4] - 1, 10),
        N,
        N,
    ]


# ---- reading from a lake -------------------------------------------------------------------


def test_panel_from_lake_reads_warmup_before_the_window(tmp_path):
    from stonks.store.lake import DuckDBLake

    raw = _random_bars(40, seed=5)
    with DuckDBLake(tmp_path / "lake.duckdb") as lake:
        lake.migrate()
        prices = raw.rename(columns={"timestamp": "date"})
        prices["date"] = prices["date"].dt.date
        lake.upsert_prices(prices)
        dates = pd.bdate_range("2024-01-01", periods=40)
        request = PanelRequest(("A", "B"), dates[20].date(), dates[30].date())
        panel = panel_from_lake(parse_factor("Mean($close, 10)"), lake, request)
    assert panel.index[0] == dates[20]
    assert panel.index[-1] == dates[30]
    assert panel.notna().all().all()
    closes = raw[raw["ticker"] == "A"].set_index("timestamp")["close"]
    assert panel.loc[dates[20], "A"] == pytest.approx(closes.iloc[11:21].mean())


def test_panel_from_a_point_in_time_view_matches_the_lake(tmp_path):
    from stonks.store.lake import DuckDBLake
    from stonks.store.pit import PointInTimeLake

    raw = _random_bars(40, seed=8)
    with DuckDBLake(tmp_path / "lake.duckdb") as lake:
        lake.migrate()
        prices = raw.rename(columns={"timestamp": "date"})
        prices["date"] = prices["date"].dt.date
        lake.upsert_prices(prices)
        dates = pd.bdate_range("2024-01-01", periods=40)
        node = parse_factor("CSRank($close / Ref($close, 5))")
        request = PanelRequest(("A", "B", "C", "D"), dates[10].date(), dates[39].date())
        full = panel_from_lake(node, lake, request)
        view = PointInTimeLake(lake, dates[25].to_pydatetime())
        seen = panel_from_lake(node, view, request)
    assert seen.index[-1] == dates[25]
    pd.testing.assert_frame_equal(seen, full.loc[: dates[25]], check_freq=False)


def test_warmup_days_cover_weekends_and_intraday_sessions():
    assert warmup_days(0, Interval.DAY_1) == 0
    assert warmup_days(20, Interval.DAY_1) >= 28
    assert warmup_days(12, Interval.parse("1h")) >= 3
    assert warmup_days(4, Interval.parse("1w")) >= 28


def test_panel_request_validates():
    with pytest.raises(ValueError, match="empty"):
        PanelRequest((), date(2024, 1, 1), date(2024, 2, 1))
    with pytest.raises(ValueError, match="after"):
        PanelRequest(("A",), date(2024, 2, 1), date(2024, 1, 1))
    assert PanelRequest(("A", "A", "B"), date(2024, 1, 1), date(2024, 1, 1)).universe == ("A", "B")


def test_empty_input_gives_an_empty_panel():
    out = evaluate(parse("$close"), prepare_bars(RAW.iloc[0:0]), ["A"])
    assert out.empty and list(out.columns) == ["A"]


def test_a_correlation_with_a_constant_series_is_empty_not_nan():
    """DuckDB's CORR is NaN when one side is constant. The module rule is
    that a non-finite value is empty, inside the expression too: a NaN
    must not rank first in CSRank or pass a comparison."""
    raw = _bars(
        {"A": [1, 2, 3, 4, 5, 6], "B": [6, 5, 4, 3, 2, 1], "C": [1, 3, 2, 4, 3, 5]},
        {"C": [100.0, 110.0, 105.0, 120.0, 90.0, 130.0], "D": [100.0] * 6},
    )
    raw = pd.concat([raw, _bars({"D": [1, 2, 4, 3, 5, 4]}, {"D": [100.0] * 6})])
    corr = "Corr($close, $volume, 3)"
    assert _col(_panel(f"CSRank({corr})", raw), "D") == [N] * 6
    assert _col(_panel(f"{corr} > 0.99", raw), "D") == [N] * 6
    assert _col(_panel(corr, raw), "D") == [N] * 6


def test_the_r_squared_of_a_flat_window_is_empty():
    """A flat close has no trend to explain: REGR_R2 says 1.0, the best
    score a trend-quality factor can give, but the value is undefined."""
    raw = _bars({"A": [1, 2, 3, 4, 5, 6], "F": [5, 5, 5, 5, 5, 5]})
    assert _col(_panel("Rsquare($close, 3)", raw), "F") == [N] * 6
    assert _col(_panel("CSRank(Rsquare($close, 3))", raw), "F") == [N] * 6
