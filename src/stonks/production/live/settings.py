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

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SubmitSettings(BaseModel):
    """``[production.live.submit]``: when the ``live_submit`` job sends the
    tickets a live book decided after the close (roadmap 19.8)."""

    model_config = ConfigDict(extra="forbid")

    #: The exchange calendar whose next open the window leads up to.
    calendar: str = "XNYS"
    #: The window opens this many minutes before the next session's open.
    #: The ``live_submit`` job fires at the same moment.
    window_minutes: int = Field(default=20, ge=1, le=600)
    #: Tickets expire this many minutes before the open: the last moment an
    #: opening-auction order is still taken. A ticket not sent by then is
    #: never sent late, and the next tick decides afresh.
    deadline_minutes: int = Field(default=2, ge=0, le=120)

    @model_validator(mode="after")
    def _window_before_deadline(self) -> SubmitSettings:
        if self.deadline_minutes >= self.window_minutes:
            raise ValueError("deadline_minutes must be less than window_minutes")
        return self


class LiveSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The broker account is shared with the owner's own trading. Stonks
    #: only trades the positions it opened (ownership by attribution), and
    #: reconciliation does not count the owner's positions or orders as
    #: drift. ``false`` treats any position or order Stonks did not make as
    #: drift (roadmap 19.5).
    allow_manual_trades: bool = True
    #: Live books decide after the close and write order tickets, which the
    #: ``live_submit`` job sends in the submit window (roadmap 19.8). Off:
    #: an auto book sends its orders at once, as before. Books with an
    #: ``approve`` subscription, and every close of a runaway run, always
    #: use tickets.
    submit_in_window: bool = False
    submit: SubmitSettings = Field(default_factory=SubmitSettings)
    #: Reconciliation checks in a row that could not reach the broker, on
    #: this many distinct sessions, before the portfolio's auto
    #: subscriptions pause. A shorter outage only skips the day (19.5).
    outage_pause_after_sessions: int = Field(default=2, ge=1)
    #: A short opening order for a name the borrow source marks hard to
    #: borrow, or whose yearly borrow fee is at or above this fraction,
    #: waits for a person as a ticket held ``hard_to_borrow``, even in an
    #: auto book (roadmap 19.16).
    hard_to_borrow_fee_rate: float = Field(default=0.03, ge=0.0)
    #: The start-of-day check cancels day and opening-auction orders still
    #: working from an earlier session, so yesterday's decision never
    #: fills late.
    cancel_stale_orders: bool = True
