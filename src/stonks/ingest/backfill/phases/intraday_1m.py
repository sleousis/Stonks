"""Intraday phase: one minute bars for the common stocks and ETFs of the
intraday exchanges (docs/backfill.md)."""

from __future__ import annotations

from stonks.ingest.backfill.phases._intraday import IntradayPhase


class MinutePhase(IntradayPhase):
    phase_id = "intraday_1m"
    order = 62
    title = "One minute bars"
    interval_code = "1m"
    batch_size = 1
