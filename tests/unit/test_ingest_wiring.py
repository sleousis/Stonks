"""Ops wiring: ``[ingest.quality]``, ``[ingest.fallback]`` and ``[backup]``
in Settings, and the one ingest-pipeline builder every entrypoint uses."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.config import Settings
from stonks.ingest.quality_config import DataQualityConfig, FallbackConfig
from stonks.ingest.wiring import build_ingest_pipeline
from stonks.ops.config import BackupConfig
from stonks.scheduling.calendar import TickerSessionCalendar


class _Source:
    def __init__(self, source_id: str) -> None:
        self.source_id = source_id


def test_defaults():
    s = Settings()
    assert s.ingest.quality == DataQualityConfig()
    assert s.ingest.fallback == FallbackConfig()
    assert s.backup == BackupConfig()


def test_sections_parse():
    s = Settings(
        ingest={"quality": {"spike_sigmas": 8.0}, "fallback": {"sources": {"eodhd": "yahoo"}}},
        backup={"dir": "/backups", "retention": {"daily": 3}},
    )
    assert s.ingest.quality.spike_sigmas == 8.0
    assert s.ingest.fallback.for_primary("eodhd") == "yahoo"
    assert s.backup.retention.daily == 3


def test_pipeline_gets_quality_calendar_notifier_and_no_fallback_by_default():
    settings = Settings(notify={"backends": ["log"]})
    pipeline = build_ingest_pipeline(settings, _Source("eodhd"), lake=None)
    assert pipeline._quality.config == settings.ingest.quality
    assert isinstance(pipeline._quality.calendar, TickerSessionCalendar)
    assert pipeline._fallback is None
    assert pipeline._notifier is not None


def test_pipeline_builds_the_configured_fallback():
    settings = Settings(ingest={"fallback": {"sources": {"eodhd": "yahoo"}}})
    built: list[str] = []

    def factory(source_id, sources):
        built.append(source_id)
        return _Source(source_id)

    pipeline = build_ingest_pipeline(settings, _Source("eodhd"), lake=None, source_factory=factory)
    assert built == ["yahoo"] and pipeline._fallback.source_id == "yahoo"
    # no fallback for a primary that isn't mapped
    other = build_ingest_pipeline(settings, _Source("yahoo"), lake=None, source_factory=factory)
    assert other._fallback is None


def test_an_unbuildable_fallback_is_skipped_not_fatal():
    from stonks.ingest.sources.registry import SourceConfigError

    def factory(source_id, sources):
        raise SourceConfigError("no key")

    settings = Settings(ingest={"fallback": {"sources": {"yahoo": "eodhd"}}})
    pipeline = build_ingest_pipeline(settings, _Source("yahoo"), lake=None, source_factory=factory)
    assert pipeline._fallback is None


def test_ticker_sessions_follow_the_exchange_calendar():
    cal = TickerSessionCalendar()
    days = list(cal.sessions("AAPL.US", date(2026, 1, 1), date(2026, 1, 9)))
    # New Year's Day and the weekend are not NYSE sessions
    assert days == [date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7),
                    date(2026, 1, 8), date(2026, 1, 9)]  # fmt: skip
    assert cal.sessions("NOSUFFIX", date(2026, 1, 1), date(2026, 1, 9)) is None
    assert cal.sessions("X.NOWHERE", date(2026, 1, 1), date(2026, 1, 9)) is None


@pytest.mark.parametrize("ticker", ["BTC-USD.CC"])
def test_crypto_trades_every_day(ticker):
    days = TickerSessionCalendar().sessions(ticker, date(2026, 1, 1), date(2026, 1, 4))
    assert len(list(days)) == 4
