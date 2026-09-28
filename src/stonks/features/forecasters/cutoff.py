"""The pretraining cutoff rule (roadmap 23.11).

A pretrained model may have seen every price up to its cutoff, so a
validation window that starts on or before it is not out-of-sample
evidence. This mirrors the AI research loop's model cutoff
(``[assistant.research] model_cutoff``).

A strategy that uses forecasters says which through a classmethod
``forecast_models(params) -> names``: the models it may run given the
pinned params (all the choices of a tunable model parameter). The lab
preflight checks each of them against the validation window.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from stonks.features.forecasters.registry import get_forecaster_class

__all__ = ["CutoffViolation", "cutoff_violations", "strategy_forecast_models"]


@dataclass(frozen=True)
class CutoffViolation:
    model: str
    #: The model's cutoff, or ``None`` when the name is not registered.
    cutoff: date | None


def strategy_forecast_models(
    strategy: Any, params: Mapping[str, Any] | None = None
) -> tuple[str, ...]:
    """The forecasters ``strategy`` (a class or an instance) may run."""
    hook = getattr(strategy, "forecast_models", None)
    if hook is None:
        return ()
    return tuple(dict.fromkeys(str(m) for m in hook(dict(params or {}))))


def cutoff_violations(models: Iterable[str], validation_start: date) -> list[CutoffViolation]:
    """Models whose cutoff is on or after ``validation_start``, plus any
    unknown name (it fails closed: an unknown model has no known cutoff)."""
    out = []
    for name in models:
        try:
            cutoff = get_forecaster_class(name).cutoff()
        except ValueError:
            out.append(CutoffViolation(name, None))
            continue
        if cutoff is not None and validation_start <= cutoff:
            out.append(CutoffViolation(name, cutoff))
    return out
