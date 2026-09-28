"""``[streaming]`` settings (roadmap 21.1). Off by default.

Example::

    [streaming]
    enabled = true
    source = "eodhd"
    tickers = ["AAPL.US", "MSFT.US", "BTC-USD.CC"]

    [streaming.eodhd]
    quotes = true

    [streaming.record]
    enabled = true
    dir = "data/streams"

    [streaming.monitor]
    deadman_minutes = 5

No key lives here. The EODHD key comes from ``EODHD_API_KEY`` (the same
one the REST ingest reads) and the IBKR login stays in the gateway.
"""

from __future__ import annotations

from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

BarMode = Literal["auto", "trades", "quotes"]

_SECRET_KEYS = ("api_key", "api_token", "token", "password")


def _refuse_secrets(data: Any) -> Any:
    if not isinstance(data, dict):
        return data
    table = cast("dict[str, Any]", data)
    found = [k for k in _SECRET_KEYS if k in table]
    if found:
        raise ValueError(
            f"{', '.join(found)} must not be set in [streaming]: keys come from the environment"
        )
    return table


class StreamBackoffSettings(BaseModel):
    """Reconnect delays: ``initial * multiplier ** n``, capped at ``max``,
    each spread by up to ``jitter`` of itself."""

    model_config = ConfigDict(extra="forbid")

    initial_seconds: float = Field(default=1.0, gt=0)
    max_seconds: float = Field(default=60.0, gt=0)
    multiplier: float = Field(default=2.0, ge=1.0)
    jitter: float = Field(default=0.2, ge=0.0, le=1.0)


class EodhdStreamSettings(BaseModel):
    """EODHD's websocket feeds. A ticker's suffix picks its feed."""

    model_config = ConfigDict(extra="forbid")

    url: str = "wss://ws.eodhistoricaldata.com/ws"
    #: Ticker suffix (``AAPL.US`` has ``US``) to EODHD feed name.
    feeds: dict[str, str] = Field(
        default_factory=lambda: {"US": "us", "CC": "crypto", "FOREX": "forex"}
    )
    #: Also subscribe US tickers to the ``us-quote`` feed (bid and ask).
    quotes: bool = False
    open_timeout_seconds: float = Field(default=10.0, gt=0)

    @model_validator(mode="before")
    @classmethod
    def _no_secrets(cls, data: Any) -> Any:
        return _refuse_secrets(data)


class IbkrStreamSettings(BaseModel):
    """Live prices through the IBKR adapter's market data snapshots."""

    model_config = ConfigDict(extra="forbid")

    #: The gateway to use (``[brokers.ibkr.gateways.<name>]``), else the only one.
    gateway: str | None = None
    #: Seconds between two snapshot requests.
    poll_seconds: float = Field(default=5.0, gt=0)


class ReplayStreamSettings(BaseModel):
    """Play a recording back as the source."""

    model_config = ConfigDict(extra="forbid")

    path: str = "data/streams"
    #: ``None`` replays as fast as possible, ``1.0`` at recorded speed.
    speed: float | None = Field(default=None, gt=0)


class StreamRecordSettings(BaseModel):
    """Save every event of a running stream to Parquet."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    dir: str = "data/streams"
    #: A chunk file is written after this many events or seconds.
    chunk_events: int = Field(default=5000, ge=1)
    chunk_seconds: float = Field(default=60.0, gt=0)


class StreamMonitorSettings(BaseModel):
    """Engine monitoring (roadmap 21.3.4): the status row, its staleness and
    the engine dead-man."""

    model_config = ConfigDict(extra="forbid")

    #: Alert the operator when no bar close was dispatched for this long
    #: while the engine's market is open.
    deadman_minutes: int = Field(default=5, gt=0)
    #: An engine that has not written its status for this long is not live.
    stale_after_seconds: float = Field(default=120.0, gt=0)
    #: The engine writes its status at most this often.
    publish_seconds: float = Field(default=15.0, gt=0)


class StreamingSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    #: A registered streaming source (``python -m stonks.streaming sources``).
    source: str = "eodhd"
    tickers: list[str] = Field(default_factory=list)
    #: The exchange calendar that says when a silent stream is stale.
    calendar: str = "XNYS"
    #: ``trades`` builds bars from trades only, ``quotes`` from the last
    #: price or the mid, ``auto`` from trades, and from quotes for tickers
    #: that never traded on the stream (forex, IBKR snapshots).
    bar_mode: BarMode = "auto"
    #: A bar closes this long after its minute ends, for late ticks.
    bar_grace_seconds: float = Field(default=2.0, ge=0)
    #: Closed bars are written after this many seconds or bars.
    flush_seconds: float = Field(default=5.0, gt=0)
    flush_bars: int = Field(default=500, ge=1)
    #: A source yields a heartbeat after this long without data.
    heartbeat_seconds: float = Field(default=5.0, gt=0)
    #: No data for this long while the market is open counts as a disconnect.
    stale_after_seconds: float = Field(default=60.0, gt=0)
    #: Fill a gap from the REST intraday ingest once data flows again.
    backfill: bool = True
    #: The REST source for backfills (``--source`` ids of the ingest).
    backfill_source: str = "eodhd"
    #: Shorter gaps are not backfilled.
    min_gap_seconds: float = Field(default=60.0, ge=0)
    #: A gap is backfilled this long after data flows again, so the vendor
    #: has finished the minute the stream came back in.
    backfill_delay_seconds: float = Field(default=120.0, ge=0)
    backoff: StreamBackoffSettings = Field(default_factory=StreamBackoffSettings)
    eodhd: EodhdStreamSettings = Field(default_factory=EodhdStreamSettings)
    ibkr: IbkrStreamSettings = Field(default_factory=IbkrStreamSettings)
    replay: ReplayStreamSettings = Field(default_factory=ReplayStreamSettings)
    record: StreamRecordSettings = Field(default_factory=StreamRecordSettings)
    monitor: StreamMonitorSettings = Field(default_factory=StreamMonitorSettings)

    @model_validator(mode="before")
    @classmethod
    def _no_secrets(cls, data: Any) -> Any:
        return _refuse_secrets(data)
