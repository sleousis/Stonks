"""Unit tests for the ingest bar validators (roadmap 12.5)."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from stonks.core.interval import Interval
from stonks.ingest.quality import BarQualityChecker
from stonks.ingest.quality_config import DataQualityConfig, FallbackConfig

D1 = Interval.DAY_1


def _frame(closes, *, start=date(2025, 1, 1), ticker="AAA.US", volume=1000, days=None):
    days = days or [start + timedelta(days=i) for i in range(len(closes))]
    rows = []
    for d, c in zip(days, closes, strict=True):
        rows.append(
            {
                "ticker": ticker,
                "timestamp": pd.Timestamp(d),
                "open": c,
                "high": c * 1.01,
                "low": c * 0.99,
                "close": c,
                "adj_close": c,
                "volume": volume,
            }
        )
    return pd.DataFrame(rows)


def _walk(n, seed=1, start=100.0, vol=0.01):
    rng = np.random.default_rng(seed)
    return list(start * np.exp(np.cumsum(rng.normal(0, vol, n))))


def _checker(**overrides):
    return BarQualityChecker(DataQualityConfig(**overrides))


def _reasons(result):
    return {i: r for i, r in result.reasons.items() if r}


def _codes(result):
    return {w.code for w in result.warnings}


def test_clean_series_passes():
    frame = _frame(_walk(60))
    result = _checker().check(frame, interval=D1, as_of=date(2025, 3, 1))
    assert _reasons(result) == {}
    assert result.rejected == 0


@pytest.mark.parametrize(
    ("column", "value", "reason"),
    [
        ("close", 0.0, "non_positive_price"),
        ("low", -1.0, "non_positive_price"),
        ("close", float("nan"), "missing_price"),
        ("high", 50.0, "high_below_low"),
        ("close", 150.0, "close_out_of_range"),
    ],
)
def test_row_rules_quarantine(column, value, reason):
    frame = _frame([100.0] * 5)
    frame.loc[2, column] = value
    result = _checker().check(frame, interval=D1)
    assert reason in _reasons(result)[2].split(",")
    assert set(_reasons(result)) == {2}


def test_close_within_rounding_tolerance_is_kept():
    frame = _frame([100.0] * 5)
    frame.loc[2, "close"] = frame.loc[2, "high"] * 1.0001
    assert _reasons(_checker().check(frame, interval=D1)) == {}


def test_duplicate_timestamp_keeps_last():
    frame = _frame([100.0, 101.0, 102.0])
    dup = frame.iloc[[1]].copy()
    dup["close"] = 101.5
    frame = pd.concat([frame, dup], ignore_index=True)
    result = _checker().check(frame, interval=D1)
    assert _reasons(result) == {1: "duplicate_timestamp"}


def test_one_bar_spike_that_reverts_is_quarantined():
    closes = _walk(60)
    closes[40] = closes[39] * 3.0
    result = _checker().check(_frame(closes), interval=D1)
    assert _reasons(result) == {40: "price_spike"}


def test_large_move_that_holds_is_only_a_warning():
    closes = _walk(60)
    closes = closes[:40] + [c * 0.4 for c in closes[40:]]  # a real crash
    result = _checker().check(_frame(closes), interval=D1)
    assert _reasons(result) == {}
    assert "extreme_move" in _codes(result)


def test_split_is_not_a_spike_or_move():
    closes = _walk(60)
    closes = closes[:40] + [c / 4 for c in closes[40:]]
    frame = _frame(closes)
    split_day = frame.loc[40, "timestamp"].date()
    splits = pd.DataFrame([{"ex_date": split_day, "ratio": 4.0}])
    result = _checker().check(frame, interval=D1, splits=splits)
    assert _reasons(result) == {}
    assert "extreme_move" not in _codes(result)


def test_reverse_split_then_regular_trading_is_clean():
    # 1:10 reverse split: price x10, ratio 0.1 new shares per old.
    closes = _walk(60)
    closes = closes[:30] + [c * 10 for c in closes[30:]]
    frame = _frame(closes)
    splits = pd.DataFrame([{"ex_date": frame.loc[30, "timestamp"].date(), "ratio": 0.1}])
    result = _checker().check(frame, interval=D1, splits=splits)
    assert _reasons(result) == {}
    assert "extreme_move" not in _codes(result)


def test_vendor_adjusted_close_explains_jump_without_split_table():
    closes = _walk(60)
    frame = _frame(closes[:40] + [c / 2 for c in closes[40:]])
    frame["adj_close"] = [c / 2 for c in closes[:40]] + [c / 2 for c in closes[40:]]
    result = _checker().check(frame, interval=D1)
    assert _reasons(result) == {}
    assert "extreme_move" not in _codes(result)


def test_spike_detection_uses_stored_history():
    history = _frame(_walk(50), start=date(2024, 11, 1))
    last = float(history["close"].iloc[-1])
    batch = _frame([last, last * 3, last * 1.001], start=date(2024, 12, 25))
    result = _checker().check(batch, interval=D1, history=history)
    assert _reasons(result) == {1: "price_spike"}


def test_too_little_history_skips_spike_rule():
    result = _checker().check(_frame([100.0, 300.0, 100.0]), interval=D1)
    assert _reasons(result) == {}


def test_spike_on_last_bar_is_a_warning_not_quarantine():
    closes = _walk(40)
    closes[-1] = closes[-2] * 3
    result = _checker().check(_frame(closes), interval=D1)
    assert _reasons(result) == {}
    assert "extreme_move" in _codes(result)


def test_zero_volume_streak_warns():
    frame = _frame(_walk(20))
    frame.loc[5:11, "volume"] = 0
    result = _checker(zero_volume_streak=5).check(frame, interval=D1)
    assert _reasons(result) == {}
    [w] = [w for w in result.warnings if w.code == "zero_volume_streak"]
    assert w.count == 7


def test_flat_price_streak_warns():
    result = _checker(flat_price_streak=10).check(_frame([50.0] * 12), interval=D1)
    assert "flat_price_streak" in _codes(result)


def test_stale_series_warns_against_as_of():
    frame = _frame(_walk(10), start=date(2025, 1, 1))
    assert "stale_series" in _codes(_checker().check(frame, interval=D1, as_of=date(2025, 2, 1)))
    assert "stale_series" not in _codes(
        _checker().check(frame, interval=D1, as_of=date(2025, 1, 12))
    )


def test_stale_rule_is_daily_only():
    frame = _frame(_walk(10))
    result = _checker().check(frame, interval=Interval.HOUR_1, as_of=date(2026, 1, 1))
    assert "stale_series" not in _codes(result)


class _WeekdayCalendar:
    def sessions(self, ticker, start, end):
        days = pd.bdate_range(start, end)
        return [d.date() for d in days]


class _UnknownCalendar:
    def sessions(self, ticker, start, end):
        return None


def test_calendar_gap_warns_when_a_calendar_is_given():
    days = [d.date() for d in pd.bdate_range("2025-01-06", periods=10)]
    del days[4]
    frame = _frame(_walk(9), days=days)
    result = BarQualityChecker(DataQualityConfig(), calendar=_WeekdayCalendar()).check(
        frame, interval=D1
    )
    [w] = [w for w in result.warnings if w.code == "calendar_gap"]
    assert w.count == 1
    assert _reasons(result) == {}


def test_calendar_without_sessions_is_ignored():
    days = [d.date() for d in pd.bdate_range("2025-01-06", periods=10)]
    del days[4]
    frame = _frame(_walk(9), days=days)
    for cal in (_UnknownCalendar(), object()):
        result = BarQualityChecker(DataQualityConfig(), calendar=cal).check(frame, interval=D1)
        assert "calendar_gap" not in _codes(result)


def test_disabled_config_passes_everything():
    frame = _frame([100.0, -1.0, 100.0])
    result = _checker(enabled=False).check(frame, interval=D1)
    assert _reasons(result) == {}


def test_empty_frame():
    result = _checker().check(_frame([]).reindex(columns=list(_frame([1.0]).columns)), interval=D1)
    assert result.rejected == 0
    assert result.warnings == []


def test_intraday_timestamps():
    closes = _walk(60)
    closes[30] = closes[29] * 3
    start = datetime(2025, 1, 2, 14, 30)
    days = [start + timedelta(hours=i) for i in range(60)]
    result = _checker().check(_frame(closes, days=days), interval=Interval.HOUR_1)
    assert _reasons(result) == {30: "price_spike"}


def test_squeeze_that_only_partly_reverts_is_kept():
    # GME-like: +135% then -44% next day. Real, so only a warning.
    closes = _walk(60)
    closes[40] = closes[39] * 2.35
    closes[41] = closes[40] * 0.56
    closes[42:] = [c * closes[41] / closes[42] for c in closes[42:]]
    result = _checker().check(_frame(closes), interval=D1)
    assert _reasons(result) == {}
    assert "extreme_move" in _codes(result)


@pytest.mark.parametrize("asset_class", ["bond", "commodity"])
def test_non_positive_values_allowed_where_they_are_real(asset_class):
    # Negative bond yields (2019 Bunds), negative oil futures (April 2020).
    closes = [0.5, 0.2, -0.1, -0.3, 0.1] * 6
    frame = _frame(closes)
    frame["high"] = frame[["close"]].max(axis=1) + 0.05
    frame["low"] = frame[["close"]].min(axis=1) - 0.05
    frame["open"] = frame["close"]
    result = _checker().check(frame, interval=D1, asset_class=asset_class)
    assert _reasons(result) == {}
    equity = _checker().check(frame, interval=D1, asset_class="equity")
    assert "non_positive_price" in set(_reasons(equity).values())


def test_spike_rule_is_skipped_for_bonds():
    closes = _walk(60, start=2.0)
    closes[40] = closes[39] * 3
    result = _checker().check(_frame(closes), interval=D1, asset_class="bond")
    assert _reasons(result) == {}


def test_run_quality_breaches_respect_thresholds():
    from stonks.ingest.quality import RunQuality, SeriesWarning

    q = RunQuality(bars_checked=10, bars_quarantined=2)
    q.warnings = [SeriesWarning("A", "stale_series", 9), SeriesWarning("B", "calendar_gap", 1)]
    q.supplied_by = {"A": "yahoo"}
    cfg = DataQualityConfig(alert_quarantined_rows=2, alert_warned_tickers=2)
    assert len(q.breaches(cfg)) == 3
    quiet = DataQualityConfig(
        alert_quarantined_rows=0, alert_warned_tickers=0, alert_on_fallback=False
    )
    assert q.breaches(quiet) == []
    assert q.to_dict()["warnings"] == {"calendar_gap": 1, "stale_series": 1}


def test_fallback_config_lookup():
    cfg = FallbackConfig(sources={"eodhd": "yahoo", "yahoo": "yahoo"})
    assert cfg.for_primary("eodhd") == "yahoo"
    assert cfg.for_primary("yahoo") is None
    assert cfg.for_primary("defillama") is None


# ---- stored spikes (DS-03) ------------------------------------------------------------


def test_a_stored_last_bar_spike_reverted_by_the_batch_is_reported():
    # daily incremental ingest: the spike arrived alone yesterday and was
    # stored; today's one-bar batch takes it back
    closes = _walk(61)
    closes[60] = closes[59] * 3.0
    history = _frame(closes, start=date(2024, 11, 1))
    batch = _frame([closes[59] * 1.001], start=date(2024, 11, 1) + timedelta(days=61))
    result = _checker().check(batch, interval=D1, history=history)
    spike_ts = history["timestamp"].iloc[-1]
    assert result.stored_spikes == [("AAA.US", spike_ts)]
    assert _reasons(result) == {}
    assert "extreme_move" not in _codes(result)


def test_a_stored_move_that_holds_is_not_a_stored_spike():
    closes = _walk(61)
    closes[60] = closes[59] * 3.0
    history = _frame(closes, start=date(2024, 11, 1))
    batch = _frame([closes[60] * 1.001], start=date(2024, 11, 1) + timedelta(days=61))
    result = _checker().check(batch, interval=D1, history=history)
    assert result.stored_spikes == []


# ---- non-positive prices with an overlapping config (DS-12) ----------------------------


def test_negative_prices_do_not_crash_the_spike_rule_when_configs_overlap():
    closes = [50.0, 40.0, 20.0, -5.0, -37.0, 10.0, 20.0] * 5
    frame = _frame(closes)
    frame["high"] = frame["close"] + 1
    frame["low"] = frame["close"] - 1
    frame["open"] = frame["close"]
    checker = _checker(spike_asset_classes=["equity", "crypto", "commodity"])
    result = checker.check(frame, interval=D1, asset_class="commodity")
    assert result.rejected == 0


# ---- gaps and staleness use the clean rows (DS-19) ---------------------------------------


def test_a_day_whose_only_bar_is_quarantined_is_a_calendar_gap():
    days = [d.date() for d in pd.bdate_range("2025-01-06", periods=10)]
    frame = _frame(_walk(10), days=days)
    frame.loc[4, "close"] = 0.0
    result = BarQualityChecker(DataQualityConfig(), calendar=_WeekdayCalendar()).check(
        frame, interval=D1
    )
    assert set(_reasons(result)) == {4}
    [w] = [w for w in result.warnings if w.code == "calendar_gap"]
    assert w.count == 1


def test_stale_rule_ignores_a_quarantined_newest_bar():
    frame = _frame(_walk(10), start=date(2025, 1, 1))
    frame.loc[9, "close"] = float("nan")
    # clean bars end on 9 January; the rejected one on the 10th
    result = _checker(stale_after_days=7).check(frame, interval=D1, as_of=date(2025, 1, 17))
    assert "stale_series" in _codes(result)


# ---- edge cases -------------------------------------------------------------------------


def test_one_row_batches_pass_without_history():
    result = _checker().check(_frame([100.0]), interval=D1)
    assert result.rejected == 0


def test_one_row_batch_spike_with_history_is_a_warning():
    history = _frame(_walk(50), start=date(2024, 11, 1))
    last = float(history["close"].iloc[-1])
    batch = _frame([last * 3], start=date(2024, 12, 21))
    result = _checker().check(batch, interval=D1, history=history)
    assert result.rejected == 0 and result.stored_spikes == []
    assert "extreme_move" in _codes(result)


def test_all_nan_adj_close_is_not_a_reason_to_reject():
    closes = _walk(60)
    closes[40] = closes[39] * 3.0
    frame = _frame(closes)
    frame["adj_close"] = float("nan")
    result = _checker().check(frame, interval=D1)
    assert _reasons(result) == {40: "price_spike"}


def test_duplicate_timestamp_where_the_later_copy_is_bad():
    frame = _frame([100.0, 101.0, 102.0])
    dup = frame.iloc[[1]].copy()
    dup["close"] = 0.0
    frame = pd.concat([frame, dup], ignore_index=True)
    result = _checker().check(frame, interval=D1)
    # the bad copy is rejected on its own merit and the good one is kept,
    # so the day is not lost
    assert _reasons(result) == {3: "non_positive_price,close_out_of_range"}


def test_a_split_and_a_spike_on_the_same_day():
    walk = _walk(60)
    closes = walk[:40] + [c / 4 for c in walk[40:]]
    adj = [c / 4 for c in walk]
    closes[40] *= 3.0  # a bad tick on the ex-date
    adj[40] *= 3.0
    frame = _frame(closes)
    frame["adj_close"] = adj
    splits = pd.DataFrame([{"ex_date": frame.loc[40, "timestamp"].date(), "ratio": 4.0}])
    result = _checker().check(frame, interval=D1, splits=splits)
    assert _reasons(result) == {40: "price_spike"}
