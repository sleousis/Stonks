"""Informational signal-IC survival test (BL-33).

Runs :func:`stonks.lab.signal_eval.signal_ic` on the validation window
(embargoed for the strategy; ``window="full"`` for the whole dataset) and
reports the IC by horizon, ICIR, HAC t-stats, quantile spreads, turnover
and ``ic_estimate``. It never fails a run: the IC informs sizing (BL-08's
``alpha`` normalisation) and research, while the gates are the event
study, the vs-random test and the rest of the suite. A universe under
``signal_eval.MIN_UNIVERSE`` tickers is reported as ``n/a``.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from stonks.core.protocols import Strategy, SurvivalReport
from stonks.lab.dataset import scoring_window
from stonks.lab.signal_eval import DEFAULT_HORIZONS, signal_ic


class SignalICOptions(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    horizons: tuple[int, ...] = Field(
        default=DEFAULT_HORIZONS, description="Bars ahead at which to compare scores with returns."
    )
    every_bars: int = Field(default=5, ge=1, description="Score the strategy every this many bars.")
    n_quantiles: int = Field(
        default=5, ge=2, le=10, description="How many groups to rank tickers into by score."
    )
    window: Literal["val", "full"] = Field(
        default="val",
        description="Which data to test on: val is the held-out window, full is all of it.",
    )
    #: Worker processes; ``None`` means ``lab.parallel.default_max_workers()``.
    max_workers: int | None = Field(default=None, ge=1)

    @field_validator("horizons")
    @classmethod
    def _horizons(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if not value or min(value) < 1:
            raise ValueError("horizons must be a non-empty list of bar counts >= 1")
        return tuple(sorted(set(value)))


class SignalICTest:
    id = "signal_ic"
    Options = SignalICOptions

    def __init__(self, options: SignalICOptions | None = None, **overrides: Any) -> None:
        base = options or SignalICOptions()
        self.options = (
            SignalICOptions.model_validate({**base.model_dump(), **overrides})
            if overrides
            else base
        )

    @classmethod
    def build(cls, options: SignalICOptions) -> SignalICTest:
        return cls(options)

    def run(self, strategy: Strategy, context: Any) -> SurvivalReport:
        opts = self.options
        window = scoring_window(context, strategy, opts.window)
        result = signal_ic(
            strategy,
            context,
            opts.horizons,
            opts.every_bars,
            window=window,
            n_quantiles=opts.n_quantiles,
            max_workers=opts.max_workers,
        )
        span = f"window {window[0]}..{window[1]}"
        if result.status != "ok":
            notes = f"n/a: {result.note}; {span}"
        else:
            notes = (
                f"informational; ic_estimate {result.ic_estimate:+.4f} at "
                f"h={result.ic_horizon}; {span}"
            )
        return SurvivalReport(self.id, True, result.metrics(), notes)
