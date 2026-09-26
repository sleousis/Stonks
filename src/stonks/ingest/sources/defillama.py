"""DefiLlama data source: daily DeFi total value locked (TVL) per chain.

DefiLlama's free API needs no key. The only capability this adapter serves
is :meth:`DataSource.fetch_chain_tvl`, backed by
``GET {base_url}/v2/historicalChainTvl/{chain}`` which returns the chain's
full daily history as ``[{"date": <unix seconds, 00:00 UTC>, "tvl": <USD>}]``
(liquid staking and double-counted TVL excluded, per the vendor docs at
https://api-docs.defillama.com/). The endpoint has no date filter, so
``since`` is applied client-side.

Vendor details stay here: ``date``/``tvl`` become the domain columns
``observation_date``/``tvl_usd`` and chain names are normalized to the
canonical lower-case form (``"Ethereum"`` -> ``"ethereum"``) before rows
leave the module. Every other DataSource capability raises
:class:`UnsupportedCapabilityError` (a soft-fail in the pipeline).

HTTP follows the EODHD adapter's pattern: a per-request timeout, retries
with exponential backoff only on transient failures (connection errors,
timeouts, HTTP 429 / 5xx), everything else raised on the first attempt.
An unknown chain answers 404 and maps to :class:`DefiLlamaUnknownChainError`.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Iterator
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import quote

import requests

from stonks.ingest.redact import format_exception
from stonks.ingest.schemas import DefiTvlRow, FinancialStatementsBundle, RawPriceBar
from stonks.ingest.sources.base import DataSource, DataSourceError, UnsupportedCapabilityError
from stonks.logging import get_logger

DEFAULT_BASE_URL = "https://api.llama.fi"

_log = get_logger("stonks.ingest.sources.defillama")


class DefiLlamaResponseError(DataSourceError):
    """The vendor answered with something that is not a TVL series."""


class DefiLlamaUnknownChainError(DataSourceError):
    """DefiLlama does not track the requested chain (HTTP 404)."""


def normalize_chain(raw: str) -> str:
    """Canonical chain key: trimmed, lower-case, inner whitespace collapsed."""
    chain = " ".join(str(raw).split()).lower()
    if not chain:
        raise ValueError("chain must be non-empty")
    return chain


def _observation_date(raw: Any) -> date | None:
    if isinstance(raw, bool):
        return None
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        return None
    try:
        return datetime.fromtimestamp(seconds, UTC).date()
    except (OverflowError, OSError, ValueError):
        return None


def _tvl(raw: Any) -> float | None:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value != value or value < 0 or value == float("inf"):
        return None
    return value


def parse_chain_tvl_response(chain: str, payload: Any, *, source: str) -> Iterator[DefiTvlRow]:
    """Map a ``historicalChainTvl`` payload onto :class:`DefiTvlRow`.

    Rows without a parseable date or a finite, non-negative TVL are dropped
    (and counted in one log line). When the vendor repeats a day the last
    value wins. Output is sorted by ``observation_date``.
    """
    if not isinstance(payload, list):
        raise DefiLlamaResponseError(
            f"expected a list of TVL points for {chain!r}, got {type(payload).__name__}"
        )
    canonical = normalize_chain(chain)
    by_day: dict[date, float] = {}
    dropped = 0
    for item in payload:
        if not isinstance(item, dict):
            dropped += 1
            continue
        day = _observation_date(item.get("date"))
        value = _tvl(item.get("tvl"))
        if day is None or value is None:
            dropped += 1
            continue
        by_day[day] = value
    if dropped:
        _log.info(
            "defillama.parse.dropped_rows", chain=canonical, kept=len(by_day), dropped=dropped
        )
    for day in sorted(by_day):
        yield DefiTvlRow(chain=canonical, observation_date=day, tvl_usd=by_day[day], source=source)


_RETRYABLE_TRANSPORT_ERRORS = (
    requests.ConnectionError,
    requests.Timeout,
    requests.exceptions.ChunkedEncodingError,
)


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, _RETRYABLE_TRANSPORT_ERRORS):
        return True
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        status = exc.response.status_code
        return status == 429 or status >= 500
    return False


class DefiLlamaDataSource(DataSource):
    source_id = "defillama"

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: int = 30,
        max_retries: int = 3,
        retry_backoff_seconds: float = 1.0,
        session: requests.Session | None = None,
    ):
        if max_retries < 1:
            raise ValueError(f"max_retries must be >= 1, got {max_retries}")
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._backoff = retry_backoff_seconds
        self._session = session or requests.Session()
        self._log = _log

    # ---- the one capability --------------------------------------------------

    def fetch_chain_tvl(self, chain: str, since: date | None = None) -> Iterable[DefiTvlRow]:
        canonical = normalize_chain(chain)
        url = f"{self._base_url}/v2/historicalChainTvl/{quote(canonical, safe='')}"
        payload = self._get(url, chain=canonical)
        rows = parse_chain_tvl_response(canonical, payload, source=self.source_id)
        return [r for r in rows if since is None or r.observation_date >= since]

    # ---- unsupported capabilities -------------------------------------------

    def list_tickers(self, exchange: str) -> list[str]:
        raise UnsupportedCapabilityError(f"defillama has no ticker listing ({exchange!r})")

    def fetch_prices(
        self, ticker: str, since: date | None = None, until: date | None = None
    ) -> Iterable[RawPriceBar]:
        raise UnsupportedCapabilityError(f"defillama does not serve prices ({ticker!r})")

    def fetch_fundamentals(self, ticker: str) -> FinancialStatementsBundle:
        raise UnsupportedCapabilityError(f"defillama does not serve fundamentals ({ticker!r})")

    # ---- HTTP ----------------------------------------------------------------

    def _get(self, url: str, *, chain: str) -> Any:
        for attempt in range(1, self._max_retries + 1):
            try:
                return self._get_once(url, chain=chain)
            except DataSourceError:
                raise
            except Exception as exc:
                retry = _is_retryable(exc) and attempt < self._max_retries
                self._log.warning(
                    "defillama.request.failed",
                    url=url,
                    attempt=attempt,
                    max_retries=self._max_retries,
                    retrying=retry,
                    error=format_exception(exc),
                )
                if not retry:
                    raise
                time.sleep(self._backoff * (2 ** (attempt - 1)))
        raise AssertionError("unreachable: max_retries must be >= 1")

    def _get_once(self, url: str, *, chain: str) -> Any:
        response = self._session.get(url, timeout=self._timeout)
        if response.status_code == 404:
            raise DefiLlamaUnknownChainError(f"DefiLlama does not track chain {chain!r}")
        response.raise_for_status()
        try:
            return response.json()
        except ValueError:
            snippet = response.text.strip()[:200]
            raise DefiLlamaResponseError(
                f"undecodable TVL response for {chain!r}: {snippet!r}"
            ) from None
