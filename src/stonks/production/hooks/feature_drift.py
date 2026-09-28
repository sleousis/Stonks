"""Live feature drift of model strategies as a ``tick``-stage hook
(roadmap 23.10). Warn only: findings other than ``ok`` land in the tick
summary under ``feature_drift`` and in the log. Skipped in a dry run.

Settings come from ``[production.feature_drift]`` through ``TickSettings``.
A context without them gets the defaults."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from stonks.production.feature_drift import FeatureDriftSettings, check_feature_drift
from stonks.production.hooks import PostTickHook, TickHookContext, register_hook


def _settings(settings: Any) -> FeatureDriftSettings:
    value = getattr(settings, "feature_drift", None)
    if value is None:
        return FeatureDriftSettings()
    if isinstance(value, FeatureDriftSettings):
        return value
    if isinstance(value, Mapping):
        return FeatureDriftSettings.model_validate(dict(value))
    return FeatureDriftSettings.model_validate(value, from_attributes=True)


@register_hook
class FeatureDriftHook(PostTickHook):
    name = "feature_drift"
    stage = "tick"
    order = 75

    def run(self, ctx: TickHookContext) -> Mapping[str, Any] | None:
        if ctx.dry_run or ctx.signals is None or not ctx.signals.instances:
            return None
        signals = ctx.signals
        lake = signals.lake_view if signals.lake_view is not None else ctx.lake
        findings = check_feature_drift(
            ctx.state,
            lake,
            ctx.as_of,
            signals.instances,
            list(signals.universe),
            _settings(ctx.settings),
        )
        warnings = [f.as_dict() for f in findings if f.status != "ok"]
        return {"feature_drift": warnings} if warnings else None
