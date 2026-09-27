"""``[production.live]``: settings of live trading that are not risk rules
(roadmap Phase 19).

The live safeguards' limits are risk rules and sit under
``[production.risk.rules.<rule>]`` (``capital_ramp``,
``live_notional_caps``, ``price_band``, ``max_orders_per_run``,
``account_rules``), so portfolio overrides can only tighten them. The
amount Stonks may trade per live portfolio is not a setting: the owner
sets it by hand (``production.live.allocation``).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class LiveSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The broker account is shared with the owner's own trading. Stonks
    #: only trades the positions it opened (ownership by attribution), and
    #: reconciliation does not count the owner's positions or orders as
    #: drift. ``false`` treats any position or order Stonks did not make as
    #: drift (roadmap 19.5).
    allow_manual_trades: bool = True
