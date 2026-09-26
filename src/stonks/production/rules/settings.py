"""Settings of the W3.1 risk rules (BL-27), one model per rule, grouped in
``RuleSettings``. Every rule is off by default.

The rules read them as ``policy.rules.<rule name>``: the integration step
adds ``rules: RuleSettings = RuleSettings()`` to ``RiskPolicy`` (config
``[production.risk.rules.<rule name>]``) and registers
:func:`tighter_rule_settings` as its ``accounts.book.MERGE_RULES`` entry,
so portfolio and subscription overrides can only tighten them.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict

from stonks.production.rules.circuit_breaker import CircuitBreakerSettings, Cooldown
from stonks.production.rules.drawdown_scaling import DrawdownScalingSettings, Schedule
from stonks.production.rules.liquidity import LiquiditySettings
from stonks.production.rules.max_holding import MaxHoldingSettings
from stonks.production.rules.operational_halt import OperationalHaltSettings
from stonks.production.rules.portfolio_vol import PortfolioVolSettings
from stonks.production.rules.risk_per_position import RiskPerPositionSettings
from stonks.production.rules.sector_cap import SectorCapSettings

__all__ = [
    "CircuitBreakerSettings",
    "DrawdownScalingSettings",
    "LiquiditySettings",
    "MaxHoldingSettings",
    "OperationalHaltSettings",
    "PortfolioVolSettings",
    "RiskPerPositionSettings",
    "RuleSettings",
    "SectorCapSettings",
    "merge_schedules",
    "tighter_rule_settings",
]


class RuleSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_holding: MaxHoldingSettings = MaxHoldingSettings()
    drawdown_scaling: DrawdownScalingSettings = DrawdownScalingSettings()
    portfolio_vol: PortfolioVolSettings = PortfolioVolSettings()
    risk_per_position: RiskPerPositionSettings = RiskPerPositionSettings()
    sector_cap: SectorCapSettings = SectorCapSettings()
    liquidity: LiquiditySettings = LiquiditySettings()
    circuit_breaker: CircuitBreakerSettings = CircuitBreakerSettings()
    operational_halt: OperationalHaltSettings = OperationalHaltSettings()


def _min_optional(a: float | None, b: float | None) -> float | None:
    """``None`` means no limit, so any number is tighter."""
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def _max_optional(a: float | None, b: float | None) -> float | None:
    """For floors: ``None`` means no floor, a higher floor is tighter."""
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def _size_at(schedule: Schedule, drawdown: float) -> float:
    return min((size for dd, size in schedule if dd <= drawdown), default=1.0)


def _stricter_cooldown(a: Cooldown, b: Cooldown) -> Cooldown:
    """Holding a halt for the rest of the month is the stricter cooldown."""
    return "rest_of_month" if "rest_of_month" in (a, b) else "none"


def merge_schedules(a: Schedule | None, b: Schedule | None) -> Schedule | None:
    """At every threshold of either schedule, the smaller size of the two."""
    if a is None:
        return b
    if b is None:
        return a
    thresholds = sorted({dd for dd, _ in a} | {dd for dd, _ in b})
    return tuple((t, min(_size_at(a, t), _size_at(b, t))) for t in thresholds)


#: How each field of each rule's settings tightens.
MERGE_RULES: dict[str, dict[str, Callable[[Any, Any], Any]]] = {
    "max_holding": {"max_holding_bars": _min_optional},
    "drawdown_scaling": {"schedule": merge_schedules},
    "portfolio_vol": {"vol_cap": _min_optional, "shock_cap": _min_optional},
    "risk_per_position": {
        "max_risk": _min_optional,
        "atr_multiple": max,
        "max_var": _min_optional,
    },
    "sector_cap": {"max_weight_per_sector": _min_optional},
    "liquidity": {
        "max_pct_adv": _min_optional,
        "min_median_dollar_volume": _max_optional,
        "max_amihud": _min_optional,
    },
    "circuit_breaker": {
        "max_month_loss": _min_optional,
        "max_week_loss": _min_optional,
        "max_drawdown_halt": _min_optional,
        "week_sessions": max,
        "cooldown": _stricter_cooldown,
    },
    "operational_halt": {"max_bar_age_days": _min_optional},
}


def tighter_rule_settings(
    base: RuleSettings, override: RuleSettings | Mapping[str, Any] | None
) -> RuleSettings:
    """``base`` tightened by ``override`` (a ``RuleSettings`` or a mapping of
    one, validated). Only the fields the override sets explicitly take
    part; none can loosen ``base``."""
    if override is None:
        return base
    if not isinstance(override, RuleSettings):
        override = RuleSettings.model_validate(dict(override))
    values: dict[str, Any] = {}
    for rule in override.model_fields_set:
        current, given = getattr(base, rule), getattr(override, rule)
        merged = {
            name: MERGE_RULES[rule][name](getattr(current, name), getattr(given, name))
            for name in given.model_fields_set
        }
        values[rule] = type(current).model_validate({**current.model_dump(), **merged})
    merged_settings = base.model_copy(update=values)
    return base if merged_settings == base else merged_settings
