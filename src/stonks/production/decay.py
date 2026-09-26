"""The alpha-decay monitor (BL-47, roadmap 9.5.4; Tulchinsky, *Finding
Alphas*): is a live strategy still earning what its backtest promised?

It reads a sleeve's daily attributed returns (``realized_return`` of its
``risk_snapshots`` rows, the hypothetical return of yesterday's holdings)
and computes:

- ``ir_short`` and ``ir_long``: the annualised information ratio (mean over
  standard deviation, against cash) of the last 60 and 120 returns;
- ``days_negative``: how many trailing days the rolling short IR has stayed
  below zero.

It fires when ``days_negative`` reaches ``negative_days`` (20), or when the
long IR falls below ``min_fraction`` (50%) of the expected IR: the backtest's
out-of-sample Sharpe (``oos.sharpe_oos``), else its benchmark-relative IR.
Until a full short window exists nothing is judged.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "DecayCheck",
    "DecaySettings",
    "evaluate_decay",
    "expected_ir",
    "rolling_ir",
    "trailing_negative_days",
]

PERIODS_PER_YEAR = 252


class DecaySettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    short_window: int = Field(default=60, ge=5)
    long_window: int = Field(default=120, ge=5)
    #: Trailing days of a negative short IR that fire the monitor.
    negative_days: int = Field(default=20, ge=1)
    #: The long IR below this share of the expected IR fires it too.
    min_fraction: float = Field(default=0.5, ge=0.0, le=1.0)


@dataclass(frozen=True)
class DecayCheck:
    ir_short: float | None
    ir_long: float | None
    expected_ir: float | None
    days_negative: int
    decayed: bool
    reason: str | None


def _ir(window: np.ndarray) -> float | None:
    sd = float(window.std(ddof=1))
    if not sd > 0 or not math.isfinite(sd):
        return None
    return float(window.mean()) / sd * math.sqrt(PERIODS_PER_YEAR)


def rolling_ir(returns: Sequence[float], window: int) -> float | None:
    """Annualised IR of the last ``window`` returns, ``None`` before a full
    window or without any variation."""
    arr = np.asarray(returns, dtype=float)
    if len(arr) < window:
        return None
    return _ir(arr[-window:])


def trailing_negative_days(returns: Sequence[float], window: int) -> int:
    """Consecutive most recent days whose rolling ``window`` IR is below 0."""
    arr = np.asarray(returns, dtype=float)
    days = 0
    for end in range(len(arr), window - 1, -1):
        ir = _ir(arr[end - window : end])
        if ir is None or ir >= 0:
            break
        days += 1
    return days


def _number(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def expected_ir(reports: Mapping[str, Mapping[str, Any]]) -> float | None:
    """What the backtest promised, from the latest survival metrics per test."""
    oos = _number((reports.get("oos") or {}).get("sharpe_oos"))
    if oos is not None:
        return oos
    return _number((reports.get("benchmark_relative") or {}).get("information_ratio"))


def evaluate_decay(
    returns: Sequence[float], expected: float | None, settings: DecaySettings
) -> DecayCheck:
    """Judge a sleeve's daily returns (oldest first) against ``expected``."""
    ir_short = rolling_ir(returns, settings.short_window)
    ir_long = rolling_ir(returns, settings.long_window)
    days = trailing_negative_days(returns, settings.short_window)
    reasons: list[str] = []
    if ir_short is not None and days >= settings.negative_days:
        reasons.append(
            f"the {settings.short_window}-day IR has stayed below zero for {days} days"
            f" (now {ir_short:.2f})"
        )
    if (
        ir_long is not None
        and expected is not None
        and expected > 0
        and ir_long < settings.min_fraction * expected
    ):
        reasons.append(
            f"the {settings.long_window}-day IR {ir_long:.2f} is below"
            f" {settings.min_fraction:.0%} of the backtest's {expected:.2f}"
        )
    return DecayCheck(
        ir_short=ir_short,
        ir_long=ir_long,
        expected_ir=expected,
        days_negative=days,
        decayed=bool(reasons),
        reason="; ".join(reasons) or None,
    )
