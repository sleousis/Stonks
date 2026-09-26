"""``ConstructionSettings`` (``[production.construction]``): which
constructor sizes a book and how its targets become orders.

It lives apart from :mod:`stonks.portfolio.pipeline` so ``stonks.config``
can mount it without importing the pipeline (which imports the config)."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from stonks.portfolio.base import PortfolioConstructor, get_constructor

SINGLE_WINNER = "single_winner"
#: Keys of a construction mapping that configure the pipeline itself; every
#: other key is a knob of the constructor.
PIPELINE_KEYS = frozenset({"method", "signal_method", "buffer_fraction", "min_trade_weight"})


class ConstructionSettings(BaseModel):
    """``[production.construction]``, merged per portfolio: which
    constructor sizes the book and how its targets become orders.

    ``params`` are the constructor's own knobs (validated by its
    ``Settings``); ``signal_method`` overrides the constructor's
    normalisation. ``buffer_fraction`` and ``min_trade_weight`` are the
    no-trade buffer of the target-weight route (``single_winner`` trades
    through the strategy's ``decide`` and ignores them)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    method: str = SINGLE_WINNER
    params: dict[str, Any] = Field(default_factory=dict)
    signal_method: str | None = None
    buffer_fraction: float = Field(default=0.10, ge=0.0, le=0.5)
    min_trade_weight: float = Field(default=0.005, ge=0.0)

    @model_validator(mode="after")
    def _known_constructor(self) -> ConstructionSettings:
        self.build()  # unknown names and bad knobs fail at load time
        return self

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any] | None) -> ConstructionSettings:
        """From a flat mapping (a portfolio's ``construction_json`` merged
        with the global settings): pipeline keys are fields, the rest are
        the constructor's ``params``."""
        flat = dict(mapping or {})
        params = dict(flat.pop("params", None) or {})
        params.update({k: v for k, v in flat.items() if k not in PIPELINE_KEYS})
        return cls(**{k: v for k, v in flat.items() if k in PIPELINE_KEYS}, params=params)

    @property
    def is_single_winner(self) -> bool:
        return self.method == SINGLE_WINNER

    def build(self) -> PortfolioConstructor:
        params = dict(self.params)
        if self.is_single_winner:
            # Signals arrive already filtered by the signal phase's threshold.
            params.setdefault("threshold", -math.inf)
        return get_constructor(self.method, **params)
