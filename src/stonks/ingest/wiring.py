"""The one place an entrypoint (``stonks ingest``, the REST API, the
scheduler) turns ``Settings`` into an :class:`IngestPipeline`: bar quality
checks from ``[ingest.quality]`` with the market calendars for the gap
rule, the ``[ingest.fallback]`` source for the primary, and the
``[notify]`` notifier for quality alerts."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from stonks.ingest.pipeline import IngestPipeline
from stonks.ingest.quality import BarQualityChecker
from stonks.ingest.sources.base import DataSource
from stonks.ingest.sources.registry import SourceConfigError, build_source
from stonks.logging import get_logger

if TYPE_CHECKING:
    from stonks.config import Settings
    from stonks.notify import Notifier
    from stonks.store.lake import DuckDBLake

_log = get_logger("stonks.ingest.wiring")

SourceFactory = Callable[[str, Any], DataSource]


def build_ingest_pipeline(
    settings: Settings,
    source: DataSource,
    lake: DuckDBLake | None,
    *,
    notifier: Notifier | None = None,
    source_factory: SourceFactory = build_source,
) -> IngestPipeline:
    """``notifier`` defaults to ``notifier_from_settings(settings)``. A
    fallback source that can't be built (e.g. no key) is logged and left
    out: the primary still runs."""
    from stonks.notify import notifier_from_settings
    from stonks.scheduling.calendar import TickerSessionCalendar

    return IngestPipeline(
        source=source,
        lake=lake,  # type: ignore[arg-type]
        quality=BarQualityChecker(settings.ingest.quality, calendar=TickerSessionCalendar()),
        fallback=_fallback(settings, source, source_factory),
        notifier=notifier if notifier is not None else notifier_from_settings(settings),
        adjustment_tolerance=settings.ensure.adjustment_tolerance,
    )


def _fallback(
    settings: Settings, source: DataSource, source_factory: SourceFactory
) -> DataSource | None:
    fallback_id = settings.ingest.fallback.for_primary(source.source_id)
    if fallback_id is None:
        return None
    try:
        return source_factory(fallback_id, settings.sources)
    except SourceConfigError as exc:
        _log.warning(
            "ingest.fallback_unavailable",
            primary=source.source_id,
            fallback=fallback_id,
            error=str(exc),
        )
        return None
