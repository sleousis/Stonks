"""``[connections.etoro]``: the eToro provider's app-level settings.

There is nothing secret here. The keys belong to each person and are sealed
per connection (``broker_credentials``), never read from TOML: an unknown
field such as ``api_key`` is refused. See ``docs/design/etoro.md``.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

#: eToro's own limits per user key and minute (docs: Rate Limits).
ETORO_READS_PER_MINUTE = 60
ETORO_ORDERS_PER_MINUTE = 20


class EtoroConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    base_url: str = "https://public-api.etoro.com"
    timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    #: Placing orders through a connection. Off: the provider only reads.
    trading: bool = False
    #: A connection made with Real keys may trade only when this is on (and
    #: then opens only at the Real money stages). Reads never need it.
    allow_real_money: bool = False
    #: Our own budget per connection, below eToro's 60 reads a minute.
    reads_per_minute: int = Field(default=50, ge=1, le=ETORO_READS_PER_MINUTE)
    #: The most opens, closes and cancels a connection sends a minute, below
    #: eToro's 20 (the terms ask for our own order frequency cap).
    orders_per_minute: int = Field(default=10, ge=1, le=ETORO_ORDERS_PER_MINUTE)
    #: Retries of a call eToro answered with 429 or, for reads, 5xx.
    max_retries: int = Field(default=3, ge=0, le=6)
    #: The first back-off; each retry doubles it (capped at a minute).
    backoff_seconds: float = Field(default=2.0, ge=0, le=60)
    #: Ticker -> eToro instrument id, for a mapping the symbol search
    #: cannot settle. Ids never change at eToro.
    instrument_overrides: dict[str, int] = Field(default_factory=dict[str, int])
