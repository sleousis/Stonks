"""Intraday phase: five minute bars for the common stocks and ETFs of the
intraday exchanges (docs/backfill.md)."""

from __future__ import annotations

from stonks.ingest.backfill.phases._intraday import IntradayPhase


class FiveMinutePhase(IntradayPhase):
    phase_id = "intraday_5m"
    order = 61
    title = "Five minute bars"
    interval_code = "5m"
    batch_size = 2
