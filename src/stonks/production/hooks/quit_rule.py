"""The quit rule as a ``tick``-stage hook (BL-29): see
:mod:`stonks.production.quit_rule`. Skipped in a dry run. Its settings come
from ``TickHookContext.settings.quit_rule`` when the tick passes them
(defaults otherwise); auto-demote needs ``TickHookContext.registry``."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

import pandas as pd

from stonks.production.hooks import PostTickHook, TickHookContext, register_hook
from stonks.production.prices import load_history
from stonks.production.quit_rule import QuitRuleSettings, apply_quit_rule


def quit_settings(settings: Any) -> QuitRuleSettings:
    value = getattr(settings, "quit_rule", None)
    if value is None:
        return QuitRuleSettings()
    if isinstance(value, QuitRuleSettings):
        return value
    if isinstance(value, Mapping):
        return QuitRuleSettings.model_validate(dict(value))
    return QuitRuleSettings.model_validate(value, from_attributes=True)


@register_hook
class QuitRuleHook(PostTickHook):
    name = "quit_rule"
    stage = "tick"
    order = 60

    def run(self, ctx: TickHookContext) -> Mapping[str, Any] | None:
        if ctx.dry_run:
            return None
        lake = ctx.lake

        def closes(tickers: Sequence[str], as_of: date, bars: int) -> Mapping[str, pd.DataFrame]:
            return load_history(lake, tickers, as_of, bars=bars)

        checks = apply_quit_rule(
            ctx.state, closes, ctx.as_of, quit_settings(ctx.settings), registry=ctx.registry
        )
        breached = [c.as_dict() for c in checks if c.breached]
        return {"quit_rule": breached} if breached else None
