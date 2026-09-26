"""Source selection: map a source id (``--source eodhd``) to a configured
:class:`DataSource`.

Adding a source is one entry in :data:`_FACTORIES` plus its config
sub-model under ``[sources.<id>]``. Vendor modules are imported lazily so
picking one source never pays the import cost of another.
"""

from __future__ import annotations

from collections.abc import Callable

from stonks.config import SourcesConfig, secret_value
from stonks.ingest.sources.base import DataSource


class SourceConfigError(ValueError):
    """The requested source is unknown or missing required configuration."""


def _build_eodhd(cfg: SourcesConfig) -> DataSource:
    from stonks.ingest.sources.eodhd import EodhdDataSource

    eodhd = cfg.eodhd
    if not eodhd.api_key:
        raise SourceConfigError(
            "EODHD_API_KEY is not set (add it to .env or your shell environment)"
        )
    return EodhdDataSource(
        api_key=secret_value(eodhd.api_key),
        base_url=eodhd.base_url,
        timeout_seconds=eodhd.timeout_seconds,
        max_retries=eodhd.max_retries,
        retry_backoff_seconds=eodhd.retry_backoff_seconds,
    )


def _build_yahoo(cfg: SourcesConfig) -> DataSource:
    from stonks.ingest.sources.yahoo import YahooDataSource

    yahoo = cfg.yahoo
    return YahooDataSource(
        timeout_seconds=yahoo.timeout_seconds,
        max_retries=yahoo.max_retries,
        retry_backoff_seconds=yahoo.retry_backoff_seconds,
        min_request_interval_seconds=yahoo.min_request_interval_seconds,
    )


def _build_defillama(cfg: SourcesConfig) -> DataSource:
    from stonks.ingest.sources.defillama import DefiLlamaDataSource

    llama = cfg.defillama
    return DefiLlamaDataSource(
        base_url=llama.base_url,
        timeout_seconds=llama.timeout_seconds,
        max_retries=llama.max_retries,
        retry_backoff_seconds=llama.retry_backoff_seconds,
    )


_FACTORIES: dict[str, Callable[[SourcesConfig], DataSource]] = {
    "eodhd": _build_eodhd,
    "yahoo": _build_yahoo,
    "defillama": _build_defillama,
}

SOURCE_IDS: tuple[str, ...] = tuple(_FACTORIES)
DEFAULT_SOURCE_ID = "eodhd"


def build_source(source_id: str, cfg: SourcesConfig) -> DataSource:
    factory = _FACTORIES.get(source_id)
    if factory is None:
        raise SourceConfigError(f"unknown source {source_id!r}; choose one of {list(SOURCE_IDS)}")
    return factory(cfg)
