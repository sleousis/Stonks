"""Generic properties of every registered risk rule with the W3.1 rules
switched on, the order they run in, their settings (off by default,
tighten-only merge) and how ``apply_risk`` treats them (BL-27)."""

from __future__ import annotations

import random
from dataclasses import replace
from datetime import timedelta

import pandas as pd
import pytest

from stonks.config import RiskPolicy
from stonks.core.types import Portfolio
from stonks.production.risk import apply_risk
from stonks.production.rules import registered_rules
from stonks.production.rules.drawdown_scaling import DEFAULT_SCHEDULE
from stonks.production.rules.settings import MERGE_RULES, RuleSettings, tighter_rule_settings
from tests.fixtures.risk_rules import AS_OF, Policy, bars, buy, context, sell

TICKERS = ["A.US", "B.US", "C.US", "D.US", "BTC-USD.CC", "NOHIST.US"]
CLASSES = {t: ("crypto" if t.endswith(".CC") else "equity") for t in TICKERS}
SECTORS = {"A.US": "Tech", "B.US": "Tech", "C.US": "Energy"}
W31 = ["max_holding", "drawdown_scaling", "portfolio_vol"]
W31_ORDER = ["risk_per_position", "sector_cap", "liquidity"]


def _history(rng: random.Random) -> dict[str, pd.DataFrame]:
    out = {}
    for t in TICKERS[:-1]:
        n = rng.choice([3, 30, 120, 260])
        price, closes = rng.uniform(5, 200), []
        for _ in range(n):
            price *= 1 + rng.gauss(0, rng.choice([0.005, 0.02, 0.05]))
            closes.append(price)
        out[t] = bars(
            closes,
            spread=price * 0.01,
            volume=[rng.choice([1e3, 1e5, 1e7]) for _ in range(n)],
        )
    return out


def _settings(rng: random.Random) -> RuleSettings:
    return RuleSettings.model_validate(
        {
            "risk_per_position": {
                "max_risk": rng.choice([None, 0.0025, 0.02, 0.05]),
                "max_var": rng.choice([None, 0.001, 0.02]),
            },
            "portfolio_vol": {
                "vol_cap": rng.choice([None, 0.05, 0.25]),
                "shock_cap": rng.choice([None, 0.1, 0.4]),
            },
            "drawdown_scaling": {"schedule": rng.choice([None, DEFAULT_SCHEDULE])},
            "liquidity": {
                "max_pct_adv": rng.choice([None, 0.01]),
                "min_median_dollar_volume": rng.choice([None, 1e6]),
                "max_amihud": rng.choice([None, 1e-8]),
            },
            "sector_cap": {"max_weight_per_sector": rng.choice([None, 0.1, 0.5])},
            "max_holding": {"max_holding_bars": rng.choice([None, 5, 50])},
        }
    )


def _case(seed: int):
    rng = random.Random(seed)
    hist = _history(rng)
    prices = {t: float(h["close"].iloc[-1]) for t, h in hist.items()}
    if rng.random() < 0.2:
        prices.pop("C.US")
    positions = {
        t: float(rng.choice([1, 10, 100]))
        for t in rng.sample(TICKERS, rng.randint(0, 4))
        if t != "NOHIST.US"
    }
    pf = Portfolio(cash=rng.choice([0.0, 1_000.0, 50_000.0]), positions=positions)
    orders, left = [], dict(positions)
    for i in range(rng.randint(0, 8)):
        t = rng.choice(TICKERS)
        qty = float(rng.choice([0.5, 3, 50, 1_000]))
        if rng.random() < 0.65:
            orders.append(buy(t, qty, i, tick_id="T"))
        elif left.get(t, 0.0) > 0:
            q = min(qty, left[t])
            left[t] -= q
            orders.append(sell(t, q, i, tick_id="T"))
    value = pf.total_value(prices)
    curve = [(AS_OF - timedelta(days=30 - i), value * rng.uniform(0.8, 1.3)) for i in range(30)]
    entries = {
        t: h.index[-min(len(h), rng.randint(1, 60))].date()
        for t, h in hist.items()
        if t in positions
    }
    pol = Policy(
        max_open_positions=rng.choice([None, 2]),
        max_weight_per_ticker=rng.choice([1.0, 0.3]),
        cash_buffer_fraction=rng.choice([0.0, 0.1]),
        min_order_notional=rng.choice([0.0, 50.0]),
        rules=_settings(rng),
    )
    ctx = context(
        pf,
        prices,
        pol,
        asset_classes=CLASSES,
        history=hist,
        sectors=SECTORS,
        equity_curve=curve,
        entry_dates=entries,
    )
    return orders, ctx


def _buys(orders) -> dict[str, float]:
    return {o.client_id: o.quantity for o in orders if o.side == "buy"}


def _sells(orders) -> dict[str, float]:
    return {o.client_id: o.quantity for o in orders if o.side == "sell"}


@pytest.mark.parametrize("rule", registered_rules(), ids=lambda r: r.name)
@pytest.mark.parametrize("seed", range(40))
def test_every_rule_never_grows_a_buy_and_never_blocks_a_sell(rule, seed):
    orders, ctx = _case(seed)
    kept, adjustments = rule.apply(orders, ctx)
    before, after = _buys(orders), _buys(kept)
    assert set(after) <= set(before)
    assert all(after[c] <= before[c] + 1e-9 for c in after)
    sells_before, sells_after = _sells(orders), _sells(kept)
    assert all(sells_after.get(c) == q for c, q in sells_before.items())
    new_sells = set(sells_after) - set(sells_before)
    assert all(sells_after[c] > 0 for c in new_sells)
    changed = sum(1 for c in before if after.get(c) != before[c]) + len(new_sells)
    assert len(adjustments) >= changed
    assert all(a.reason for a in adjustments)


@pytest.mark.parametrize("seed", range(40))
def test_apply_risk_is_deterministic_and_pure(seed):
    orders, ctx = _case(seed)
    snapshot = (ctx.portfolio.cash, dict(ctx.portfolio.positions))
    args = (orders, ctx.portfolio, ctx.prices, ctx.asset_classes, ctx.policy)
    one = apply_risk(*args, context=ctx)
    two = apply_risk(*args, context=ctx)
    assert one == two
    assert (ctx.portfolio.cash, dict(ctx.portfolio.positions)) == snapshot
    assert sum(q for q in _buys(one.orders).values()) <= sum(_buys(orders).values()) + 1e-9


@pytest.mark.parametrize("seed", range(20))
def test_bars_after_as_of_never_change_the_result(seed):
    orders, ctx = _case(seed)
    rng = random.Random(seed + 1_000)
    polluted = {
        t: pd.concat(
            [h, bars([rng.uniform(1, 500) for _ in range(5)], end=AS_OF + timedelta(days=14))]
        )
        for t, h in ctx.history.items()
    }
    args = (orders, ctx.portfolio, ctx.prices, ctx.asset_classes, ctx.policy)
    clean = apply_risk(*args, context=ctx)
    dirty = apply_risk(*args, context=replace(ctx, history=polluted))
    assert clean == dirty


#: The short-book rules (roadmap 16.2) run around these; see test_rules_shorts.py.
SHORT_RULES = {
    "margin_call",
    "squeeze_guard",
    "gross_exposure",
    "net_exposure",
    "short_caps",
    "borrow_check",
}


def test_rules_run_in_the_documented_order():
    names = [r.name for r in registered_rules() if r.name not in SHORT_RULES]
    assert names[:3] == W31
    caps_then_ours = names[names.index("max_weight_per_asset_class") + 1 :]
    # account_rules and max_orders_per_run are live safeguards (roadmap 19.6, 19.7)
    assert caps_then_ours == [
        *W31_ORDER,
        "cash_buffer",
        "account_rules",
        "min_order_notional",
        "max_orders_per_run",
    ]


# ---- settings ------------------------------------------------------------------------


def test_every_w31_rule_is_off_by_default():
    pol = Policy()
    for rule in registered_rules():
        if rule.name in W31 + W31_ORDER:
            assert not rule.enabled(pol), rule.name
            assert not rule.enabled(RiskPolicy()), rule.name


def test_apply_risk_does_not_list_disabled_rules_as_skipped():
    result = apply_risk(
        [buy("A.US", 1)], Portfolio(cash=100.0), {"A.US": 10.0}, CLASSES, RiskPolicy()
    )
    assert result.skipped_rules == []


def test_apply_risk_skips_enabled_history_rules_without_context():
    pol = Policy(rules=RuleSettings.model_validate({"sector_cap": {"max_weight_per_sector": 0.1}}))
    result = apply_risk([buy("A.US", 1)], Portfolio(cash=100.0), {"A.US": 10.0}, CLASSES, pol)
    assert result.skipped_rules == ["sector_cap"]
    assert result.orders[0].quantity == 1


def test_tighter_rule_settings_only_tightens_explicit_fields():
    base = RuleSettings.model_validate(
        {
            "risk_per_position": {"max_risk": 0.02, "atr_multiple": 2.0},
            "liquidity": {"min_median_dollar_volume": 1e6},
            "drawdown_scaling": {"schedule": DEFAULT_SCHEDULE},
        }
    )
    override = RuleSettings.model_validate(
        {
            "risk_per_position": {"max_risk": 0.03, "max_var": 0.01},
            "liquidity": {"min_median_dollar_volume": 5e5, "max_pct_adv": 0.05},
            "sector_cap": {"max_weight_per_sector": 0.2},
            "drawdown_scaling": {"schedule": [(0.02, 0.5)]},
        }
    )
    merged = tighter_rule_settings(base, override)
    assert merged.risk_per_position.max_risk == 0.02
    assert merged.risk_per_position.max_var == 0.01
    assert merged.risk_per_position.atr_multiple == 2.0  # not set by the override
    assert merged.liquidity.min_median_dollar_volume == 1e6
    assert merged.liquidity.max_pct_adv == 0.05
    assert merged.sector_cap.max_weight_per_sector == 0.2
    assert merged.drawdown_scaling.schedule == ((0.02, 0.5), (0.05, 0.5), (0.10, 0.5), (0.15, 0.25))
    assert tighter_rule_settings(base, override) == tighter_rule_settings(
        base, RuleSettings.model_validate(override.model_dump(exclude_unset=True))
    )


def test_tighter_rule_settings_accepts_a_mapping_and_rejects_loosening_k():
    base = RuleSettings.model_validate({"risk_per_position": {"atr_multiple": 3.0}})
    merged = tighter_rule_settings(base, {"risk_per_position": {"atr_multiple": 2.0}})
    assert merged.risk_per_position.atr_multiple == 3.0
    assert tighter_rule_settings(base, None) == base
    assert tighter_rule_settings(base, {}) == base


def test_every_settings_field_has_a_tighten_rule():
    for rule, field in RuleSettings.model_fields.items():
        assert set(MERGE_RULES[rule]) == set(field.annotation.model_fields), rule


def test_the_random_cases_exercise_every_w31_rule():
    fired: set[str] = set()
    for seed in range(200):
        orders, ctx = _case(seed)
        args = (orders, ctx.portfolio, ctx.prices, ctx.asset_classes, ctx.policy)
        fired |= {a.rule for a in apply_risk(*args, context=ctx).adjustments}
    must = {"max_holding", "drawdown_scaling", "risk_per_position", "max_weight_per_sector"}
    assert must <= fired
    assert fired & {"portfolio_vol", "correlation_shock"}
    assert fired & {"max_pct_adv", "min_median_dollar_volume", "max_amihud"}
