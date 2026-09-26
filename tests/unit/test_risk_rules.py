"""The risk-rule seam (BL-11): registry, generic properties, identical
behaviour to the pre-registry ``apply_risk``, and cost-model cash (11.7)."""

from __future__ import annotations

import random
import sys
import textwrap
from dataclasses import replace
from pathlib import Path

import pytest

from stonks.backtest.costs import AssetClassCosts, CostModelSettings
from stonks.config import RiskPolicy
from stonks.core.types import Order, Portfolio
from stonks.production import rules as rules_pkg
from stonks.production.risk import apply_risk
from stonks.production.rules import (
    OrderRule,
    RiskContext,
    RiskRule,
    discover_rules,
    register_rule,
    registered_rules,
)

TICKERS = ["A.US", "B.US", "C.US", "BTC-USD.CC", "ETH-USD.CC", "NOPRICE.US"]
CLASSES = {
    "A.US": "equity",
    "B.US": "equity",
    "C.US": "equity",
    "BTC-USD.CC": "crypto",
    "ETH-USD.CC": "crypto",
}
EPS = 1e-9


def _buy(ticker: str, qty: float, i: int = 0) -> Order:
    return Order(client_id=f"b:{ticker}:{i}", ticker=ticker, side="buy", quantity=qty)


def _sell(ticker: str, qty: float, i: int = 0) -> Order:
    return Order(client_id=f"s:{ticker}:{i}", ticker=ticker, side="sell", quantity=qty)


# ---- the pre-registry implementation, frozen as the behavioural oracle ------


def _legacy_apply_risk(orders, portfolio, prices, asset_classes, policy, slippage_bps, fee):
    if not policy.enabled:
        return list(orders), []
    adjustments: list[tuple] = []
    equity = portfolio.total_value(prices)
    positions = dict(portfolio.positions)
    cash = portfolio.cash
    kept: list[Order] = []

    def record(order, rule, new_qty, reason):
        adjustments.append(
            (order.ticker, order.side, rule, order.quantity, max(new_qty, 0.0), reason)
        )

    def clip(order, qty, max_qty, rule):
        if qty <= max_qty:
            return qty
        new_qty = max(max_qty, 0.0)
        record(order, rule, new_qty, f"quantity {qty} exceeds {rule} room {new_qty}")
        if new_qty <= EPS:
            return None
        return new_qty

    for order in (o for o in orders if o.side == "sell"):
        held = positions.get(order.ticker, 0.0)
        qty = order.quantity
        if qty > held:
            record(
                order,
                "sell_exceeds_position",
                held,
                f"sell of {qty} exceeds held {held}; clipped so it cannot open a short",
            )
            qty = held
        if qty <= EPS:
            continue
        kept.append(order if qty == order.quantity else replace(order, quantity=qty))
        remaining = held - qty
        if remaining <= EPS:
            positions.pop(order.ticker, None)
        else:
            positions[order.ticker] = remaining
        price = prices.get(order.ticker)
        if price and price > 0:
            cash += qty * price * (1 - slippage_bps / 10_000.0) - fee

    for order in (o for o in orders if o.side == "buy"):
        price = prices.get(order.ticker)
        if not price or price <= 0:
            record(order, "no_price", 0.0, "no current price; cannot size the order")
            continue
        qty = order.quantity
        held = positions.get(order.ticker, 0.0)
        if policy.max_open_positions is not None and held <= EPS:
            open_count = sum(1 for q in positions.values() if q > EPS)
            if open_count >= policy.max_open_positions:
                record(
                    order,
                    "max_open_positions",
                    0.0,
                    f"{open_count} open positions >= limit {policy.max_open_positions}",
                )
                continue
        room = policy.max_weight_per_ticker * equity - max(held, 0.0) * price
        qty = clip(order, qty, room / price, "max_weight_per_ticker")
        if qty is None:
            continue
        if policy.max_weight_per_asset_class:
            cls = asset_classes.get(order.ticker)
            if cls is None:
                record(
                    order,
                    "unknown_asset_class",
                    0.0,
                    "asset class unknown while asset-class caps are configured",
                )
                continue
            cap = policy.max_weight_per_asset_class.get(cls)
            if cap is not None:
                unpriced = sorted(
                    t
                    for t, q in positions.items()
                    if q > 0 and asset_classes.get(t) == cls and not prices.get(t)
                )
                if unpriced:
                    record(
                        order,
                        "unpriced_holding",
                        0.0,
                        f"{cls} exposure unknown: no price for held {', '.join(unpriced)}",
                    )
                    continue
                class_value = sum(
                    q * prices.get(t, 0.0)
                    for t, q in positions.items()
                    if q > 0 and asset_classes.get(t) == cls
                )
                room = cap * equity - class_value
                qty = clip(order, qty, room / price, "max_weight_per_asset_class")
                if qty is None:
                    continue
        fill_price = price * (1 + slippage_bps / 10_000.0)
        spendable = cash - policy.cash_buffer_fraction * equity - fee
        qty = clip(order, qty, spendable / fill_price, "cash_buffer")
        if qty is None:
            continue
        if qty * price < policy.min_order_notional:
            record(
                order,
                "min_order_notional",
                0.0,
                f"notional {qty * price:.2f} < min {policy.min_order_notional}",
            )
            continue
        kept.append(order if qty == order.quantity else replace(order, quantity=qty))
        positions[order.ticker] = held + qty
        cash -= qty * fill_price + fee
    return kept, adjustments


def _random_case(rng: random.Random):
    prices = {t: rng.choice([5.0, 10.0, 20.0, 50.0, 100.0]) for t in TICKERS[:-1]}
    if rng.random() < 0.2:
        prices.pop("ETH-USD.CC")  # an unpriced holding
    positions = {
        t: float(rng.choice([1, 5, 10, 40, 100]))
        for t in rng.sample(TICKERS, rng.randint(0, 4))
        if t != "NOPRICE.US"
    }
    portfolio = Portfolio(cash=rng.choice([0.0, 500.0, 2_000.0, 10_000.0]), positions=positions)
    orders: list[Order] = []
    for i in range(rng.randint(0, 7)):
        t = rng.choice(TICKERS)
        qty = float(rng.choice([0.5, 1, 3, 10, 50, 200, 1_000]))
        orders.append(_buy(t, qty, i) if rng.random() < 0.65 else _sell(t, qty, i))
    policy = RiskPolicy(
        enabled=rng.random() > 0.05,
        max_open_positions=rng.choice([None, None, 0, 1, 2, 3]),
        max_weight_per_ticker=rng.choice([1.0, 1.0, 0.5, 0.2, 0.05]),
        max_weight_per_asset_class=rng.choice(
            [{}, {}, {"crypto": 0.1}, {"equity": 0.6, "crypto": 0.3}]
        ),
        cash_buffer_fraction=rng.choice([0.0, 0.0, 0.1, 0.5]),
        min_order_notional=rng.choice([0.0, 0.0, 50.0, 500.0]),
    )
    slippage = rng.choice([0.0, 0.0, 10.0, 100.0])
    fee = rng.choice([0.0, 0.0, 1.0, 10.0])
    return orders, portfolio, prices, policy, slippage, fee


@pytest.mark.parametrize("seed", range(400))
def test_apply_risk_matches_the_pre_registry_implementation(seed):
    orders, portfolio, prices, policy, slippage, fee = _random_case(random.Random(seed))
    before = (portfolio.cash, dict(portfolio.positions))
    result = apply_risk(
        orders, portfolio, prices, CLASSES, policy, slippage_bps=slippage, fee_per_trade=fee
    )
    want_orders, want_adj = _legacy_apply_risk(
        orders, portfolio, prices, CLASSES, policy, slippage, fee
    )
    assert result.orders == want_orders
    got_adj = [
        (a.ticker, a.side, a.rule, a.original_quantity, a.adjusted_quantity, a.reason)
        for a in result.adjustments
    ]
    assert got_adj == want_adj
    assert (portfolio.cash, dict(portfolio.positions)) == before  # pure


# ---- registry ----------------------------------------------------------------


def test_todays_caps_are_registered_in_order():
    names = [r.name for r in registered_rules()]
    assert names == [
        "sell_within_position",
        "require_price",
        "max_open_positions",
        "max_weight_per_ticker",
        "max_weight_per_asset_class",
        "cash_buffer",
        "min_order_notional",
    ]
    orders = [r.order for r in registered_rules()]
    assert orders == sorted(orders)


def test_register_rule_rejects_a_duplicate_name():
    registry: dict[str, type[RiskRule]] = {}

    class One(OrderRule):
        name = "dup"
        order = 1

        def check(self, order, qty, book, ctx, record):
            return qty

    register_rule(One, registry=registry)
    with pytest.raises(ValueError, match="dup"):

        class Two(One):
            pass

        register_rule(Two, registry=registry)


def test_a_new_rule_module_is_discovered_without_editing_a_list(tmp_path: Path):
    pkg = tmp_path / "dummy_rules_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "halve.py").write_text(
        textwrap.dedent(
            """
            from dataclasses import replace
            from stonks.production.rules import RiskRule, register_rule

            REG = {}

            @register_rule(registry=REG)
            class HalveBuys(RiskRule):
                name = "halve_buys"
                order = 5

                def apply(self, orders, ctx):
                    return [
                        replace(o, quantity=o.quantity / 2) if o.side == "buy" else o
                        for o in orders
                    ], []
            """
        )
    )
    sys.path.insert(0, str(tmp_path))
    try:
        import dummy_rules_pkg

        discover_rules(dummy_rules_pkg)
        from dummy_rules_pkg.halve import REG

        assert list(REG) == ["halve_buys"]
    finally:
        sys.path.remove(str(tmp_path))
        for mod in ("dummy_rules_pkg", "dummy_rules_pkg.halve"):
            sys.modules.pop(mod, None)


# ---- generic properties over every registered rule ---------------------------


def _ctx(portfolio, prices, policy, **kw) -> RiskContext:
    return RiskContext(
        portfolio=portfolio, prices=prices, asset_classes=CLASSES, policy=policy, **kw
    )


def _buy_notional(orders, prices) -> float:
    return sum(o.quantity * prices.get(o.ticker, 0.0) for o in orders if o.side == "buy")


@pytest.mark.parametrize("rule", registered_rules(), ids=lambda r: r.name)
@pytest.mark.parametrize("seed", range(60))
def test_every_rule_never_increases_buys_and_never_blocks_sells(rule, seed):
    rng = random.Random(seed)
    orders, portfolio, prices, policy, slippage, fee = _random_case(rng)
    policy = policy.model_copy(update={"enabled": True})
    # Sells within the held quantity: those must pass untouched.
    orders = [
        o
        for o in orders
        if o.side == "buy" or 0 < o.quantity <= portfolio.positions.get(o.ticker, 0.0)
    ]
    ctx = _ctx(portfolio, prices, policy, slippage_bps=slippage, fee_per_trade=fee)
    kept, _ = rule.apply(orders, ctx)
    assert _buy_notional(kept, prices) <= _buy_notional(orders, prices) + 1e-9
    assert sorted((o.client_id, o.quantity) for o in kept if o.side == "sell") == sorted(
        (o.client_id, o.quantity) for o in orders if o.side == "sell"
    )


# ---- rules that need history ---------------------------------------------------


class _NeedsHistory(RiskRule):
    name = "needs_history"
    order = 1_000
    needs_history = True

    def apply(self, orders, ctx):
        return [o for o in orders if o.side == "sell"], []


def test_history_rules_are_skipped_without_context(monkeypatch):
    monkeypatch.setattr(rules_pkg, "registered_rules", lambda: [*_real_rules(), _NeedsHistory()])
    result = apply_risk(
        [_buy("A.US", 1)], Portfolio(cash=100.0), {"A.US": 10.0}, CLASSES, RiskPolicy()
    )
    assert [o.ticker for o in result.orders] == ["A.US"]
    assert result.skipped_rules == ["needs_history"]


def test_history_rules_run_with_a_context(monkeypatch):
    monkeypatch.setattr(rules_pkg, "registered_rules", lambda: [*_real_rules(), _NeedsHistory()])
    pf = Portfolio(cash=100.0)
    prices = {"A.US": 10.0}
    ctx = _ctx(pf, prices, RiskPolicy())
    result = apply_risk([_buy("A.US", 1)], pf, prices, CLASSES, RiskPolicy(), context=ctx)
    assert result.orders == []
    assert result.skipped_rules == []


_REAL_RULES = registered_rules()


def _real_rules():
    return list(_REAL_RULES)


# ---- cost-model cash (roadmap 11.7) -------------------------------------------


def _fees(bps: float = 0.0, flat: float = 0.0, spread: float = 0.0) -> CostModelSettings:
    return CostModelSettings(
        default=AssetClassCosts(fee_bps=bps, fee_flat=flat, half_spread_bps=spread)
    )


def test_cash_buffer_uses_the_cost_model_fee_bps():
    result = apply_risk(
        [_buy("A.US", 1_000)],
        Portfolio(cash=1_000.0),
        {"A.US": 10.0},
        CLASSES,
        RiskPolicy(),
        cost_model=_fees(bps=100.0),  # 1% of notional
    )
    [order] = result.orders
    assert order.quantity * 10.0 * 1.01 <= 1_000.0 + 1e-9
    assert order.quantity == pytest.approx(1_000.0 / 10.1, rel=1e-9)
    assert result.adjustments[-1].rule == "cash_buffer"


def test_cash_buffer_uses_the_cost_model_spread_and_flat_fee():
    result = apply_risk(
        [_buy("A.US", 1_000)],
        Portfolio(cash=1_000.0),
        {"A.US": 10.0},
        CLASSES,
        RiskPolicy(cash_buffer_fraction=0.1),
        cost_model=_fees(flat=5.0, spread=100.0),  # buy at 10.1, 5 per trade
    )
    # (1000 - 100 buffer - 5 fee) / 10.1
    assert result.orders[0].quantity == pytest.approx(895.0 / 10.1, rel=1e-9)


def test_cost_model_nets_sell_proceeds_before_funding_buys():
    result = apply_risk(
        [_sell("A.US", 100), _buy("B.US", 1_000)],
        Portfolio(cash=0.0, positions={"A.US": 100.0}),
        {"A.US": 10.0, "B.US": 10.0},
        CLASSES,
        RiskPolicy(),
        cost_model=_fees(bps=100.0),
    )
    # proceeds 1000 - 10 fee = 990; buys pay 1% too.
    assert result.orders[1].quantity == pytest.approx(990.0 / 10.1, rel=1e-9)


def test_cost_model_impact_uses_bar_volumes():
    settings = CostModelSettings(impact_bps=10_000.0, max_impact_bps=5_000.0)
    free = apply_risk(
        [_buy("A.US", 1_000)],
        Portfolio(cash=1_000.0),
        {"A.US": 10.0},
        CLASSES,
        RiskPolicy(),
        cost_model=settings,
    )
    costly = apply_risk(
        [_buy("A.US", 1_000)],
        Portfolio(cash=1_000.0),
        {"A.US": 10.0},
        CLASSES,
        RiskPolicy(),
        cost_model=settings,
        volumes={"A.US": 100.0},
    )
    assert free.orders[0].quantity == pytest.approx(100.0)
    q = costly.orders[0].quantity
    assert q < 100.0
    fill = 10.0 * (1 + min(10_000.0 * (q / 100.0) ** 0.5, 5_000.0) / 10_000.0)
    assert q * fill <= 1_000.0 + 1e-6


def test_zero_cost_model_matches_no_costs():
    orders = [_buy("A.US", 500), _buy("B.US", 500)]
    args = (Portfolio(cash=10_000.0), {"A.US": 10.0, "B.US": 20.0}, CLASSES)
    policy = RiskPolicy(cash_buffer_fraction=0.2)
    plain = apply_risk(orders, *args, policy)
    zero = apply_risk(orders, *args, policy, cost_model=CostModelSettings())
    assert [o.quantity for o in zero.orders] == pytest.approx([o.quantity for o in plain.orders])


def test_cost_model_and_legacy_costs_are_exclusive():
    with pytest.raises(ValueError, match="not both"):
        apply_risk(
            [_buy("A.US", 1)],
            Portfolio(cash=100.0),
            {"A.US": 10.0},
            CLASSES,
            RiskPolicy(),
            slippage_bps=5.0,
            cost_model=_fees(bps=1.0),
        )
