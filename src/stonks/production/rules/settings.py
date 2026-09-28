"""Settings of the W3.1 risk rules (BL-27), one model per rule, grouped in
``RuleSettings``. Every rule is off by default.

The rules read them as ``policy.rules.<rule name>``: ``RiskPolicy.rules``
(config ``[production.risk.rules.<rule name>]``), merged by
:func:`tighter_rule_settings` (its ``accounts.book.MERGE_RULES`` entry), so
portfolio and subscription overrides can only tighten them.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict

from stonks.production.rules._account_settings import AccountRulesSettings, longer_cycles
from stonks.production.rules._stop_settings import ProtectiveStopSettings
from stonks.production.rules.borrow_check import BorrowCheckSettings
from stonks.production.rules.capital_ramp import CapitalRampSettings
from stonks.production.rules.circuit_breaker import CircuitBreakerSettings, Cooldown
from stonks.production.rules.drawdown_scaling import DrawdownScalingSettings, Schedule
from stonks.production.rules.exposure import GrossExposureSettings, NetExposureSettings
from stonks.production.rules.intraday_drawdown import IntradayDrawdownSettings
from stonks.production.rules.intraday_loss import IntradayLossLimitSettings
from stonks.production.rules.intraday_orders import IntradayOrderRateSettings
from stonks.production.rules.intraday_stale import IntradayStaleDataSettings
from stonks.production.rules.liquidity import LiquiditySettings
from stonks.production.rules.manual_discipline import ManualDisciplineSettings
from stonks.production.rules.live_caps import LiveNotionalCapsSettings
from stonks.production.rules.margin_call import MarginCallSettings
from stonks.production.rules.max_holding import MaxHoldingSettings
from stonks.production.rules.max_orders import MaxOrdersPerRunSettings
from stonks.production.rules.operational_halt import OperationalHaltSettings
from stonks.production.rules.option_greek_limits import OptionGreekLimitsSettings
from stonks.production.rules.option_margin import OptionMarginSettings
from stonks.production.rules.option_max_loss import OptionMaxLossSettings
from stonks.production.rules.portfolio_vol import PortfolioVolSettings
from stonks.production.rules.price_band import PriceBandSettings
from stonks.production.rules.protections import (
    LosingLockSettings,
    StopCooldownSettings,
    StopGuardSettings,
)
from stonks.production.rules.risk_per_position import RiskPerPositionSettings
from stonks.production.rules.sector_cap import SectorCapSettings
from stonks.production.rules.short_caps import ShortCapsSettings
from stonks.production.rules.short_option_guard import ShortOptionGuardSettings
from stonks.production.rules.squeeze_guard import SqueezeGuardSettings
from stonks.production.rules.style_exposure import StyleExposureSettings, union_styles

__all__ = [
    "AccountRulesSettings",
    "BorrowCheckSettings",
    "CapitalRampSettings",
    "CircuitBreakerSettings",
    "DrawdownScalingSettings",
    "GrossExposureSettings",
    "IntradayDrawdownSettings",
    "IntradayLossLimitSettings",
    "IntradayOrderRateSettings",
    "IntradayStaleDataSettings",
    "LiquiditySettings",
    "LiveNotionalCapsSettings",
    "LosingLockSettings",
    "ManualDisciplineSettings",
    "MarginCallSettings",
    "MaxHoldingSettings",
    "MaxOrdersPerRunSettings",
    "NetExposureSettings",
    "OperationalHaltSettings",
    "OptionGreekLimitsSettings",
    "OptionMarginSettings",
    "OptionMaxLossSettings",
    "PortfolioVolSettings",
    "PriceBandSettings",
    "ProtectiveStopSettings",
    "RiskPerPositionSettings",
    "RuleSettings",
    "SectorCapSettings",
    "ShortCapsSettings",
    "ShortOptionGuardSettings",
    "SqueezeGuardSettings",
    "StopCooldownSettings",
    "StopGuardSettings",
    "StyleExposureSettings",
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
    # Short selling (roadmap 16.2), every one off by default.
    margin_call: MarginCallSettings = MarginCallSettings()
    squeeze_guard: SqueezeGuardSettings = SqueezeGuardSettings()
    gross_exposure: GrossExposureSettings = GrossExposureSettings()
    net_exposure: NetExposureSettings = NetExposureSettings()
    short_caps: ShortCapsSettings = ShortCapsSettings()
    borrow_check: BorrowCheckSettings = BorrowCheckSettings()
    # Options (roadmap 17.4), every one off by default.
    option_greek_limits: OptionGreekLimitsSettings = OptionGreekLimitsSettings()
    option_max_loss: OptionMaxLossSettings = OptionMaxLossSettings()
    option_margin: OptionMarginSettings = OptionMarginSettings()
    short_option_guard: ShortOptionGuardSettings = ShortOptionGuardSettings()
    # Live safeguards (roadmap 19.6): act only on books at a real broker,
    # every one off by default.
    capital_ramp: CapitalRampSettings = CapitalRampSettings()
    live_notional_caps: LiveNotionalCapsSettings = LiveNotionalCapsSettings()
    price_band: PriceBandSettings = PriceBandSettings()
    max_orders_per_run: MaxOrdersPerRunSettings = MaxOrdersPerRunSettings()
    # The account rules engine (roadmap 19.7), off by default.
    account_rules: AccountRulesSettings = AccountRulesSettings()
    # Per-strategy protections for live books (roadmap 19.6), off by default.
    stop_cooldown: StopCooldownSettings = StopCooldownSettings()
    stop_guard: StopGuardSettings = StopGuardSettings()
    losing_lock: LosingLockSettings = LosingLockSettings()
    # Style factor exposure cap (roadmap 22.4), off by default.
    style_exposure: StyleExposureSettings = StyleExposureSettings()
    # Broker-side protective stops (roadmap 19.10), off by default. Not a
    # risk rule: ``production.live.stops`` places them.
    protective_stops: ProtectiveStopSettings = ProtectiveStopSettings()
    # Intraday books (roadmap 21.3.2): act only on an event of the intraday
    # engine, every one off by default.
    intraday_loss_limit: IntradayLossLimitSettings = IntradayLossLimitSettings()
    intraday_drawdown: IntradayDrawdownSettings = IntradayDrawdownSettings()
    intraday_order_rate: IntradayOrderRateSettings = IntradayOrderRateSettings()
    intraday_stale_data: IntradayStaleDataSettings = IntradayStaleDataSettings()
    # Limits a person sets on their own manual orders (roadmap 23.4), off
    # by default.
    manual_discipline: ManualDisciplineSettings = ManualDisciplineSettings()


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


def _either(a: bool, b: bool) -> bool:
    """Switching a guard on is tighter."""
    return a or b


def _both(a: bool, b: bool) -> bool:
    """Allowing something (margin accounts) needs both to allow it."""
    return a and b


def _more_counting(a: bool | None, b: bool | None) -> bool | None:
    """``count_losses``: always counting losses (``True``) is the tightest,
    then the automatic choice (``None``), then never (``False``)."""
    rank = {True: 2, None: 1, False: 0}
    return a if rank[a] >= rank[b] else b


def _keep_base(a: Any, b: Any) -> Any:
    """Set globally only: an override can't change it."""
    return a


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
    "margin_call": {
        "enabled": _either,
        "margin": _keep_base,
        "buffer": max,
        # 19.13: a higher cushion alerts and reduces earlier
        "warn_cushion": max,
        "reduce_cushion": max,
        "restore_cushion": max,
    },
    "squeeze_guard": {
        "max_borrow_fee": _min_optional,
        "max_adverse_pct": _min_optional,
        "atr_multiple": _min_optional,
        "spike_pct": _min_optional,
        "spike_bars": max,
        "borrow": _keep_base,
    },
    "gross_exposure": {"max_gross": _min_optional},
    "net_exposure": {"min_net": _max_optional, "max_net": _min_optional},
    "short_caps": {"max_short_weight": _min_optional, "max_short_total": _min_optional},
    "borrow_check": {"enabled": _either, "max_borrow_fee": _min_optional, "borrow": _keep_base},
    "option_greek_limits": {
        "max_dollar_delta": _min_optional,
        "max_dollar_gamma": _min_optional,
        "max_vega": _min_optional,
        "max_theta": _min_optional,
        "max_position_dollar_delta": _min_optional,
        "max_position_vega": _min_optional,
    },
    "option_max_loss": {"max_loss_per_group": _min_optional, "max_loss_total": _min_optional},
    "option_margin": {"enabled": _either, "method": _keep_base, "cash_buffer": max},
    "short_option_guard": {
        "enabled": _either,
        "approval_level": min,
        "cash_secured": _either,
        "min_dte": max,
        "max_short_contracts": _min_optional,
    },
    "capital_ramp": {"enabled": _either},
    "live_notional_caps": {
        "max_order_notional": _min_optional,
        "max_day_notional": _min_optional,
        "max_user_day_notional": _min_optional,
        "max_global_day_notional": _min_optional,
    },
    "price_band": {
        "band_pct": _min_optional,
        "nbbo_band_pct": min,
        "delayed_band_pct": min,
        "max_gap_pct": _min_optional,
    },
    "max_orders_per_run": {
        "max_opening_orders": _min_optional,
        "max_closing_orders": _min_optional,
    },
    "account_rules": {
        "enabled": _either,
        "settlement_days": longer_cycles,
        "pdt_equity_threshold": max,
        "pdt_max_day_trades": min,
        "pdt_window_days": max,
        "wash_sale_window_days": max,
        "short_disclosure_threshold": min,
        # 19.13: an override may turn margin accounts off, never on.
        "margin_accounts": _both,
        "margin_buffer": max,
    },
    "stop_cooldown": {"cooldown_days": _max_optional, "count_losses": _more_counting},
    "stop_guard": {"max_stops": _min_optional, "window_days": max, "count_losses": _more_counting},
    "losing_lock": {"max_consecutive_losses": _min_optional, "lock_days": max},
    "style_exposure": {"max_abs_exposure": _min_optional, "styles": union_styles},
    "protective_stops": {
        "enabled": _either,
        "atr_multiple": min,
        "atr_window": _keep_base,
        "fallback_pct": min,
    },
    # A longer window sees a higher peak, so it catches more losses.
    "intraday_loss_limit": {
        "max_loss": _min_optional,
        "hard_loss": _min_optional,
        "window_minutes": max,
        "flatten": _either,
    },
    "intraday_drawdown": {"schedule": merge_schedules},
    "intraday_order_rate": {
        "max_orders_per_minute": _min_optional,
        "max_orders_per_day": _min_optional,
    },
    "intraday_stale_data": {"max_bar_age_seconds": _min_optional},
    "manual_discipline": {
        "enabled": _either,
        "require_stop_live": _either,
        "cooldown_minutes": _max_optional,
        "max_entries_per_day": _min_optional,
        "max_daily_loss": _min_optional,
    },
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
