"""``[production.options]``: live options at a real broker (roadmap 17.8).

Off by default. An option order opens only when all three hold:

1. ``live = true`` here;
2. the portfolio stands at ``live_small`` or higher (its live stage);
3. its owner set an options approval level above ``none``.

Closing an option position never needs them (P28): a book can always wind
down. The limits of the option risk rules stay under
``[production.risk.rules.*]``; this block only holds what is not a rule.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ExpirySettings(BaseModel):
    """``[production.options.expiry]``: what happens before an option expires."""

    model_config = ConfigDict(extra="forbid")

    #: Plan a close or a roll when this many sessions or fewer are left.
    #: 1 closes on the session before expiry, so the order works on the
    #: last day. A short option never expires unattended.
    close_sessions: int = Field(default=1, ge=0, le=20)
    #: ``close`` buys back a short and sells a long. ``roll`` closes it and
    #: opens the same right and strike at the expiry nearest
    #: ``roll_target_days`` out, as one combo.
    action: Literal["close", "roll"] = "close"
    roll_target_days: int = Field(default=35, ge=7, le=180)
    #: Long options are closed too. IBKR exercises a long option in the
    #: money by 0.01, and the shares it delivers need cash or margin.
    close_longs: bool = True
    #: On expiry day, a short option still held and in the money (or within
    #: this share of the strike of it) raises a high urgency alert.
    watch_band: float = Field(default=0.01, ge=0.0, le=0.2)


class OptionsLiveSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The master switch. ``false``: nothing about options reaches a
    #: broker, and the ``options_live`` job skips.
    live: bool = False
    #: A limit sits at the mid plus this share of the half spread (0: at
    #: the mid, 1: at the touch). Option orders are never market orders.
    collar_share: float = Field(default=0.0, ge=0.0, le=1.0)
    #: A quote whose spread is wider than this share of its mid is not
    #: priced: the order is not made, and the owner hears why.
    max_spread_pct: float = Field(default=0.5, gt=0.0, le=5.0)
    #: Every option order waits for a person (approve mode) by default.
    #: ``true`` lets closing orders (expiry closes) go out approved by the
    #: system. Opening orders always wait.
    auto_approve_closes: bool = False
    #: The max loss per option group and in total, as a share of equity,
    #: when the ``option_max_loss`` rule sets none. Live option books
    #: always run the defined-risk rules.
    max_loss_per_group: float = Field(default=0.02, gt=0.0, le=1.0)
    max_loss_total: float = Field(default=0.10, gt=0.0, le=1.0)
    #: Keep strikes within this share of spot when reading a live chain.
    chain_strike_band: float = Field(default=0.3, gt=0.0, le=2.0)
    #: Read expiries up to this many days out from a live chain.
    chain_max_expiry_days: int = Field(default=120, ge=1, le=800)
    expiry: ExpirySettings = Field(default_factory=ExpirySettings)
