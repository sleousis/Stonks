"""The one gate every live option order passes (roadmap 17.8).

An option order may **open** a position only when:

1. ``[production.options] live = true``;
2. the portfolio's live stage is ``live_small`` or higher;
3. its options approval level is above ``none``.

The structure itself (covered, spread, naked) is checked against the level
by the ``short_option_guard`` rule. A **close** always passes (P28): a
demoted book, or one whose switch was turned off, can still wind down.

:func:`options_live_state` is pure. :func:`gate_lookup` reads the three
inputs at each call, so a change takes effect on the next order. The
broker adapter calls it before it sends any option order.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from stonks.logging import get_logger
from stonks.options.live.approval import DEFAULT_LEVEL, ApprovalLevel, get_approval
from stonks.production.live.stages import REAL_MONEY
from stonks.store.state import SqliteState

_log = get_logger("stonks.options.live.gate")


@dataclass(frozen=True)
class OptionsLiveState:
    """Where a portfolio stands for live options."""

    #: ``[production.options] live``.
    enabled: bool
    #: The portfolio's live stage (``None``: unknown).
    stage: str | None
    level: ApprovalLevel
    #: Why an option order may not open (empty: it may).
    reasons: tuple[str, ...] = ()

    @property
    def allowed(self) -> bool:
        return not self.reasons

    @property
    def refusal(self) -> str | None:
        """One sentence naming every missing condition, or ``None``."""
        if not self.reasons:
            return None
        return "options may not open here: " + ", and ".join(self.reasons)


def options_live_state(enabled: bool, stage: str | None, level: ApprovalLevel) -> OptionsLiveState:
    reasons: list[str] = []
    if not enabled:
        reasons.append("options live is off ([production.options] live = false)")
    if stage not in REAL_MONEY:
        reasons.append(f"the portfolio is at stage {stage or 'unknown'}, not live_small or higher")
    if level == "none":
        reasons.append("the options approval level is none")
    return OptionsLiveState(enabled=enabled, stage=stage, level=level, reasons=tuple(reasons))


OFF = options_live_state(False, None, DEFAULT_LEVEL)

#: What the broker asks before an option order: the portfolio's state now.
OptionsGate = Callable[[], OptionsLiveState]


def configured_live() -> bool:
    """``[production.options] live`` from the settings files and the
    environment. Any failure reads as off."""
    try:
        from stonks.config import load_settings

        return bool(load_settings().production.options.live)
    except Exception as exc:  # a broken config never turns options on
        _log.warning("options.live.settings_unreadable", error=str(exc))
        return False


def gate_lookup(
    state: SqliteState,
    portfolio_id: str,
    live: Callable[[], bool] = configured_live,
) -> OptionsGate:
    """The gate of ``portfolio_id``: the switch, its stage and its level,
    read at each call. Anything unreadable reads as closed."""

    def lookup() -> OptionsLiveState:
        from stonks.production.live.stages import get_stage, stages_enabled

        try:
            enabled = bool(live())
            stage = get_stage(state, portfolio_id) if stages_enabled(state) else None
            level = get_approval(state, portfolio_id).level
        except Exception as exc:
            _log.warning("options.live.gate_unreadable", portfolio_id=portfolio_id, error=str(exc))
            return options_live_state(False, None, DEFAULT_LEVEL)
        return options_live_state(enabled, stage, level)

    return lookup
