"""The catalog of settings an admin may edit in the console.

An allowlist: a key that is not here cannot be overridden. Each entry says
which group it sits in, a label and one line of help for the console (the
risk limits use the names of the trader's own Risk limits tab, and the
console shows fractions as percents), and
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
        "Largest holding",
        "Most of a portfolio one ticker may take.",
    ),
    EditableSetting(
        f"{_RISK}.max_open_positions",
        "risk",
        "Open positions",
        "Most tickers held at once. Empty means no limit.",
    ),
    EditableSetting(
        f"{_RISK}.cash_buffer_fraction",
        "risk",
        "Cash to keep",
        "Part of a portfolio that stays in cash after buys.",
    ),
    EditableSetting(
        f"{_RISK}.min_order_notional",
        "risk",
        "Smallest order",
        "Buys worth less than this are skipped.",
    ),
    EditableSetting(
        f"{_RULES}.circuit_breaker.max_month_loss",
        "risk",
        "Monthly loss limit",
        "Stops new buys after this loss since the month began. Empty turns it off.",
    ),
    EditableSetting(
        f"{_RULES}.circuit_breaker.max_week_loss",
        "risk",
        "Weekly loss limit",
        "Stops new buys after this loss over the last five runs. Empty turns it off.",
    ),
    EditableSetting(
        f"{_RULES}.circuit_breaker.max_drawdown_halt",
        "risk",
        "Drop from the peak that halts buys",
        "Stops new buys until an admin clears it. Empty turns it off.",
    ),
    EditableSetting(
        f"{_RULES}.circuit_breaker.cooldown",
        "risk",
        "Breaker cooldown",
        "How long a tripped loss limit keeps new buys stopped.",
    ),
    EditableSetting(
        f"{_RULES}.drawdown_scaling.schedule",
        "risk",
        "Size down in a drawdown",
        "Pairs of a drop from the peak and the size of new buys, as fractions. "
        "[[0.1, 0.5]] halves new buys after a 10% drop.",
    ),
    EditableSetting(
        f"{_RULES}.sector_cap.max_weight_per_sector",
        "risk",
        "Most in one sector",
        "Most of a portfolio one sector may take. Empty means no limit.",
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
        "Stops new buys when the newest price is older than this many days. Empty is off.",
    ),
    # ---- trading run ---------------------------------------------------------------
    EditableSetting(
        "production.universe",
        "trading",
        "Trading universe",
        "The tickers the trading run looks at: a stored universe, or a list of tickers.",
    ),
    EditableSetting(
        "production.model_books",
        "trading",
        "Test books",
        "Which strategies keep a test book.",
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
        "Alerts below this level are only stored.",
    ),
    EditableSetting(
        "notify.backends",
        "notifications",
        "Where system alerts go",
        "Where operator alerts go.",
    ),
)

#: Trigger fields an admin may change per job, by trigger type. A session
#: job's label names its edge: "minutes after the close".
_TRIGGER_FIELDS: dict[str, tuple[str, str, str]] = {
    "session": ("offset_minutes", "Minutes after the {anchor}", "A negative number runs it "
                "before."),
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
            label = label.format(anchor=getattr(job.trigger, "anchor", "close"))
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
