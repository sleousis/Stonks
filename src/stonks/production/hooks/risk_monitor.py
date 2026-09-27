"""Live risk monitoring as a ``tick``-stage hook (BL-47): after a real tick,
every portfolio the tick traded gets its VaR and ES snapshot, its violation
scores and, per strategy sleeve, the alpha-decay check (see
:mod:`stonks.production.risk_metrics`). Alerts go through the configured
notification router. Skipped in a dry run.

Settings come from ``TickHookContext.settings.risk_monitor`` and
``.decay`` when the tick carries them, the defaults otherwise."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from stonks.production.decay import DecaySettings
from stonks.production.hooks import PostTickHook, TickHookContext, register_hook
from stonks.production.risk_metrics import RiskMonitorSettings, record_risk_snapshots


def _settings[T: (RiskMonitorSettings, DecaySettings)](settings: Any, name: str, cls: type[T]) -> T:
    value = getattr(settings, name, None)
    if value is None:
        return cls()
    if isinstance(value, cls):
        return value
    if isinstance(value, Mapping):
        return cls.model_validate(dict(value))
    return cls.model_validate(value, from_attributes=True)


@register_hook
class RiskMonitorHook(PostTickHook):
    name = "risk_monitor"
    stage = "tick"
    order = 70

    def run(self, ctx: TickHookContext) -> Mapping[str, Any] | None:
        if ctx.dry_run or not ctx.portfolios:
            return None
        _, alerts = record_risk_snapshots(
            ctx.state,
            ctx.lake,
            ctx.as_of,
            sorted(ctx.portfolios),
            tick_id=ctx.tick_id,
            settings=_settings(ctx.settings, "risk_monitor", RiskMonitorSettings),
            decay=_settings(ctx.settings, "decay", DecaySettings),
        )
        return {"risk_alerts": [a.as_dict() for a in alerts]} if alerts else None
