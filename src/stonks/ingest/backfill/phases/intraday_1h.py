"""Intraday phase: hourly bars for the common stocks and ETFs of the
intraday exchanges (docs/backfill.md)."""

from __future__ import annotations

from stonks.ingest.backfill.phases._intraday import IntradayPhase


class HourPhase(IntradayPhase):
    phase_id = "intraday_1h"
    order = 60
    title = "Hourly bars"
    interval_code = "1h"
    batch_size = 4
