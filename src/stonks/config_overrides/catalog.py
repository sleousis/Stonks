"""The catalog of settings an admin may edit in the console.

An allowlist: a key that is not here cannot be overridden. Each entry says
which group it sits in, a label and one line of help for the console, and
when a change takes effect: ``next_run`` for blocks that read settings on
every run (the trading run, its risk rules and notifier), ``restart`` for
the scheduler, which builds its job table when it starts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

Group = Literal["risk", "trading", "notifications", "schedule"]
Applies = Literal["next_run", "restart"]


@dataclass(frozen=True)
class EditableSetting:
    key: str
    group: Group
    label: str
    help: str
    applies: Applies = "next_run"


_RISK = "production.risk"
_RULES = f"{_RISK}.rules"

_STATIC: tuple[EditableSetting, ...] = (
    # ---- risk: the system policy every book starts from -----------------------
    EditableSetting(
        f"{_RISK}.enabled", "risk", "Risk limits on", "Turns the whole risk layer on or off."
    ),
    EditableSetting(
        f"{_RISK}.max_weight_per_ticker",
        "risk",
        "Most in one ticker",
        "Largest share of a portfolio one ticker may hold (0.25 = 25%).",
    ),
    EditableSetting(
        f"{_RISK}.max_open_positions",
        "risk",
        "Most open positions",
        "Most tickers held at once. Empty means no limit.",
    ),
    EditableSetting(
        f"{_RISK}.cash_buffer_fraction",
        "risk",
        "Cash kept aside",
        "Share of the portfolio that stays in cash after buys.",
    ),
    EditableSetting(
        f"{_RISK}.min_order_notional",
        "risk",
        "Smallest order",
        "Buys smaller than this amount are dropped.",
    ),
    EditableSetting(
        f"{_RULES}.circuit_breaker.max_month_loss",
        "risk",
        "Monthly loss limit",
        "Halts new buys after this loss from the month's start (0.06 = 6%). Empty turns it off.",
    ),
    EditableSetting(
        f"{_RULES}.circuit_breaker.max_week_loss",
        "risk",
        "Weekly loss limit",
        "Halts new buys after this loss over the last five runs. Empty turns it off.",
    ),
    EditableSetting(
        f"{_RULES}.circuit_breaker.max_drawdown_halt",
        "risk",
        "Drop from the peak that halts buys",
        "Halts new buys until an admin clears it. Empty turns it off.",
    ),
    EditableSetting(
        f"{_RULES}.circuit_breaker.cooldown",
        "risk",
        "Breaker cooldown",
        "rest_of_month holds a trip until the month ends; none lifts it once the loss is back.",
    ),
    EditableSetting(
        f"{_RULES}.drawdown_scaling.schedule",
        "risk",
        "Size down in a drawdown",
        "Pairs of [drop from the peak, size of new buys], e.g. [[0.1, 0.5], [0.2, 0]].",
    ),
    EditableSetting(
        f"{_RULES}.sector_cap.max_weight_per_sector",
        "risk",
        "Most in one sector",
        "Largest share of a portfolio one sector may hold. Empty means no limit.",
    ),
    EditableSetting(
        f"{_RULES}.max_holding.max_holding_bars",
        "risk",
        "Longest hold",
        "Sells a position held this many days. Empty means no limit.",
    ),
    EditableSetting(
        f"{_RULES}.operational_halt.max_bar_age_days",
        "risk",
        "Stale data halt",
        "Halts new buys when the newest price is older than this many days. Empty is off.",
    ),
    # ---- trading run ---------------------------------------------------------------
    EditableSetting(
        "production.universe",
        "trading",
        "Trading universe",
        "The tickers the trading run looks at: a list, or the id of a stored universe.",
    ),
    EditableSetting(
        "production.model_books",
        "trading",
        "Test books",
        "all keeps a test book for every strategy that is not retired; shadow only for "
        "strategies on trial.",
    ),
    EditableSetting(
        "production.initial_cash",
        "trading",
        "Starting cash",
        "Cash a new paper portfolio and a new test book start with.",
    ),
    EditableSetting(
        "production.max_price_staleness_days",
        "trading",
        "Oldest usable price",
        "Prices older than this many days are not used to buy.",
    ),
    # ---- notifications -----------------------------------------------------------
    EditableSetting(
        "notify.min_level",
        "notifications",
        "Lowest alert level sent",
        "info, warning or error. Lower levels are only stored.",
    ),
    EditableSetting(
        "notify.backends",
        "notifications",
        "Where system alerts go",
        "Any of log, store, outbox (admins' devices) and webhook.",
    ),
)

#: Trigger fields an admin may change per job, by trigger type.
_TRIGGER_FIELDS: dict[str, tuple[str, str, str]] = {
    "session": ("offset_minutes", "Minutes from the session", "Minutes after (or before, "
                "negative) the market open or close the job runs."),
    "daily": ("at", "Time of day", "The time the job runs, in the job's time zone (HH:MM)."),
    "interval": ("every_minutes", "Every how many minutes", "How often the job runs."),
}  # fmt: skip


def _job_settings(settings: Any) -> list[EditableSetting]:
    scheduler = getattr(settings, "scheduler", None)
    out: list[EditableSetting] = []
    for job in getattr(scheduler, "jobs", None) or []:
        base = f"scheduler.jobs.{job.name}"
        out.append(
            EditableSetting(
                f"{base}.enabled",
                "schedule",
                f"{job.name}: on",
                "Whether the scheduler runs this job.",
                "restart",
            )
        )
        field = _TRIGGER_FIELDS.get(getattr(job.trigger, "type", ""))
        if field is not None:
            name, label, text = field
            out.append(
                EditableSetting(f"{base}.trigger.{name}", "schedule", f"{job.name}: {label.lower()}",
                                text, "restart")
            )  # fmt: skip
    return out


def editable_settings(settings: Any) -> list[EditableSetting]:
    """Every key an admin may override, the schedule's jobs included."""
    return [*_STATIC, *_job_settings(settings)]


def find_setting(settings: Any, key: str) -> EditableSetting | None:
    """The catalog entry for ``key``, or ``None`` when it is not editable."""
    return next((s for s in editable_settings(settings) if s.key == key), None)
