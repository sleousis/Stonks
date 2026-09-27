"""Phase 16.2: risk rules for short books, hand-checked.

Book used below unless stated: cash 15,000, short 100 X, prices in each
test. Weights are over the value before the tick's orders.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from stonks.config import RiskPolicy
from stonks.core.types import Order, Portfolio
from stonks.execution.borrow import BorrowQuote, BorrowSettings, FlatBorrow
from stonks.execution.margin import MarginSettings, RegTMargin
from stonks.production.risk import apply_risk
from stonks.production.rules import RiskContext, registered_rules
from stonks.production.rules.settings import RuleSettings, tighter_rule_settings

AS_OF = date(2026, 3, 20)


def _policy(**rules) -> RiskPolicy:
    return RiskPolicy(rules=RuleSettings.model_validate(rules))


def _o(side: str, qty: float, ticker: str = "X", effect=None, cid: str | None = None) -> Order:
    token = {"open": {"sell": "short", "buy": "buy"}, "close": {"buy": "cover", "sell": "sell"}}
    tail = token.get(effect or "", {}).get(side, side)
    return Order(
        cid or f"2026-03-20:s:{ticker}:{tail}",
        ticker,
        side,  # type: ignore[arg-type]
        qty,
        position_effect=effect,
    )


def _run(orders, portfolio, prices, policy, **ctx):
    context = RiskContext(
        portfolio=portfolio,
        prices=prices,
        asset_classes={},
        policy=policy,
        as_of=AS_OF,
        allow_short=True,
        **ctx,
    )
    return apply_risk(orders, portfolio, prices, {}, policy, context=context)


def _bars(closes, end=AS_OF) -> pd.DataFrame:
    idx = pd.bdate_range(end=pd.Timestamp(end), periods=len(closes))
    c = np.asarray(closes, dtype=float)
    return pd.DataFrame({"open": c, "high": c * 1.01, "low": c * 0.99, "close": c}, index=idx)


# ---- defaults ------------------------------------------------------------------------------


def test_every_short_rule_is_off_by_default() -> None:
    names = {
        "margin_call",
        "squeeze_guard",
        "gross_exposure",
        "net_exposure",
        "short_caps",
        "borrow_check",
    }
    rules = [r for r in registered_rules() if r.name in names]
    assert len(rules) == len(names)
    assert not any(r.enabled(RiskPolicy()) for r in rules)


# ---- margin call ---------------------------------------------------------------------------


def test_a_margin_breach_forces_a_cover_by_hand() -> None:
    # Cash 15,000, short 100 X at 120: equity 3,000, maintenance 0.3 x
    # 12,000 = 3,600, deficit 600. Cover 600 / (0.3 x 120) = 16.67 shares.
    book = Portfolio(cash=15_000.0, positions={"X": -100.0})
    result = _run([], book, {"X": 120.0}, _policy(margin_call={"enabled": True}))
    [forced] = result.orders
    assert forced.client_id == "2026-03-20:risk.margin_call:X:cover"
    assert (forced.side, forced.position_effect) == ("buy", "close")
    assert forced.quantity == pytest.approx(600.0 / 36.0)
    assert forced.decision_context and forced.decision_context["forced"] == "margin_call"


def test_the_forced_cover_is_never_blocked_by_other_rules() -> None:
    # Every other rule at its tightest: none may cut the forced cover.
    policy = RiskPolicy(
        max_weight_per_ticker=0.0,
        cash_buffer_fraction=1.0,
        min_order_notional=1e12,
        max_open_positions=0,
        rules=RuleSettings.model_validate(
            {
                "margin_call": {"enabled": True},
                "gross_exposure": {"max_gross": 0.0},
                "net_exposure": {"min_net": 0.0, "max_net": 0.0},
                "short_caps": {"max_short_weight": 0.0, "max_short_total": 0.0},
                "borrow_check": {"enabled": True},
                "drawdown_scaling": {"schedule": [[0.01, 0.0]]},
                "circuit_breaker": {"max_drawdown_halt": 0.01},
                "operational_halt": {"max_bar_age_days": 1},
            }
        ),
    )
    book = Portfolio(cash=15_000.0, positions={"X": -100.0})
    history = {"X": _bars([50.0] * 30 + [120.0], end=AS_OF - timedelta(days=30))}
    result = _run(
        [_o("sell", 10.0, "Y", "open")],
        book,
        {"X": 120.0, "Y": 10.0},
        policy,
        history=history,
        equity_curve=[(AS_OF - timedelta(days=5), 20_000.0)],
    )
    covers = [o for o in result.orders if o.ticker == "X"]
    assert [(o.side, o.quantity) for o in covers] == [("buy", pytest.approx(600.0 / 36.0))]
    assert all(o.ticker != "Y" for o in result.orders)  # no new shorts in a breach


def test_margin_call_tops_up_a_proposed_cover_and_sells_longs_last() -> None:
    # Short 100 X at 120 (req 3,600) and long 10 L at 10 (req 25): X first.
    book = Portfolio(cash=15_100.0, positions={"X": -100.0, "L": 10.0})
    # Equity 15,100 - 12,000 + 100 = 3,200; maintenance 3,600 + 25 = 3,625;
    # deficit 425: cover 425 / 36 = 11.81 X. 5 already proposed.
    result = _run(
        [_o("buy", 5.0, effect="close")],
        book,
        {"X": 120.0, "L": 10.0},
        _policy(margin_call={"enabled": True}),
    )
    total = sum(o.quantity for o in result.orders if o.ticker == "X")
    assert total == pytest.approx(425.0 / 36.0)
    assert all(o.ticker != "L" for o in result.orders)


def test_margin_call_sells_a_long_on_a_leveraged_book() -> None:
    # Cash -800, long 100 at 10: equity 200, maintenance 250, deficit 50:
    # sell 50 / 2.5 = 20.
    book = Portfolio(cash=-800.0, positions={"L": 100.0})
    result = _run([], book, {"L": 10.0}, _policy(margin_call={"enabled": True}))
    [forced] = result.orders
    assert (forced.side, forced.quantity) == ("sell", pytest.approx(20.0))
    assert forced.client_id.endswith(":L:sell")


def test_margin_check_clips_opens_to_the_room() -> None:
    # Cash 1,000 flat: excess 1,000, a short at 100 needs 50 a share: 20.
    result = _run(
        [_o("sell", 30.0, effect="open")],
        Portfolio(cash=1_000.0),
        {"X": 100.0},
        _policy(margin_call={"enabled": True, "buffer": 0.0}),
    )
    assert [o.quantity for o in result.orders] == [pytest.approx(20.0)]
    # A close frees room: selling 10 long L at 100 frees 500 -> 10 more.
    result = _run(
        [_o("sell", 10.0, "L", "close"), _o("sell", 50.0, effect="open")],
        Portfolio(cash=0.0, positions={"L": 10.0}),
        {"X": 100.0, "L": 100.0},
        _policy(margin_call={"enabled": True}),
    )
    assert [o.quantity for o in result.orders if o.ticker == "X"] == [pytest.approx(20.0)]


def test_margin_check_uses_the_books_model_and_a_cash_model_refuses_shorts() -> None:
    result = _run(
        [_o("sell", 5.0, effect="open")],
        Portfolio(cash=1_000.0),
        {"X": 100.0},
        _policy(margin_call={"enabled": True, "margin": {"model": "cash"}}),
    )
    assert result.orders == []
    result = _run(
        [_o("sell", 5.0, effect="open")],
        Portfolio(cash=1_000.0),
        {"X": 100.0},
        _policy(margin_call={"enabled": True, "margin": {"model": "cash"}}),
        margin=RegTMargin(),
    )
    assert [o.quantity for o in result.orders] == [5.0]


# ---- exposure ------------------------------------------------------------------------------


def test_gross_exposure_scales_every_open_by_hand() -> None:
    # Cash 1,000: buy 60 A at 10 (0.6) and short 60 B at 10 (0.6): gross
    # 1.2 over a cap of 1.0, so both scale by 1.0 / 1.2 to 50.
    result = _run(
        [_o("buy", 60.0, "A", "open"), _o("sell", 60.0, "B", "open")],
        Portfolio(cash=1_000.0),
        {"A": 10.0, "B": 10.0},
        _policy(gross_exposure={"max_gross": 1.0}),
    )
    assert [o.quantity for o in result.orders] == pytest.approx([50.0, 50.0])


def test_gross_exposure_never_touches_closes() -> None:
    book = Portfolio(cash=2_000.0, positions={"A": 100.0})
    result = _run(
        [_o("sell", 50.0, "A", "close")],
        book,
        {"A": 10.0},
        _policy(gross_exposure={"max_gross": 0.1}),
    )
    assert [o.quantity for o in result.orders] == [50.0]


def test_net_exposure_limits_by_hand() -> None:
    # Cash 1,000: buy 80 A at 10 is net 0.8; max 0.5 -> 50.
    result = _run(
        [_o("buy", 80.0, "A", "open")],
        Portfolio(cash=1_000.0),
        {"A": 10.0},
        _policy(net_exposure={"max_net": 0.5}),
    )
    assert [o.quantity for o in result.orders] == pytest.approx([50.0])
    # Short 80 B is net -0.8; min -0.3 -> 30.
    result = _run(
        [_o("sell", 80.0, "B", "open")],
        Portfolio(cash=1_000.0),
        {"B": 10.0},
        _policy(net_exposure={"min_net": -0.3}),
    )
    assert [o.quantity for o in result.orders] == pytest.approx([30.0])


def test_net_exposure_settings_are_ordered() -> None:
    with pytest.raises(ValueError, match="exceeds"):
        _policy(net_exposure={"min_net": 0.5, "max_net": 0.1})


def test_no_equity_drops_the_opens() -> None:
    result = _run(
        [_o("buy", 1.0, "A", "open")],
        Portfolio(cash=0.0),
        {"A": 10.0},
        _policy(gross_exposure={"max_gross": 1.0}, net_exposure={"max_net": 1.0}),
    )
    assert result.orders == []


# ---- short caps ------------------------------------------------------------------------------


def test_short_caps_per_name_and_total_by_hand() -> None:
    # Equity 1,000. Per name 5 % = 50 = 5 shares at 10. A already short 2,
    # so 3 more. Total 6 % over A and B: A ends at 5 (50), B asks 5 (50),
    # total 100 > 60, so the opens (3 A, 5 B) scale until 60: A 2 + 3s, B 5s
    # with 20 + 30 s + 50 s = 60 -> s = 0.5.
    book = Portfolio(cash=1_020.0, positions={"A": -2.0})
    result = _run(
        [_o("sell", 10.0, "A", "open"), _o("sell", 5.0, "B", "open")],
        book,
        {"A": 10.0, "B": 10.0},
        _policy(short_caps={"max_short_weight": 0.05, "max_short_total": 0.06}),
    )
    assert [(o.ticker, o.quantity) for o in result.orders] == [
        ("A", pytest.approx(1.5)),
        ("B", pytest.approx(2.5)),
    ]


def test_short_caps_leave_covers_and_longs_alone() -> None:
    book = Portfolio(cash=1_200.0, positions={"A": -20.0})
    orders = [_o("buy", 20.0, "A", "close"), _o("buy", 10.0, "B", "open")]
    result = _run(
        orders, book, {"A": 10.0, "B": 10.0}, _policy(short_caps={"max_short_weight": 0.0})
    )
    assert [o.quantity for o in result.orders] == [20.0, 10.0]


def test_a_short_at_the_cap_is_dropped_and_unpriced_shorts_too() -> None:
    result = _run(
        [_o("sell", 1.0, "A", "open"), _o("sell", 1.0, "Q", "open")],
        Portfolio(cash=1_000.0),
        {"A": 10.0},
        _policy(short_caps={"max_short_weight": 0.0}),
    )
    assert result.orders == []


# ---- borrow check -------------------------------------------------------------------------------


def test_borrow_check_by_hand() -> None:
    borrow = FlatBorrow(
        overrides={
            "NONE": BorrowQuote("none"),
            "DEAR": BorrowQuote("hard", 0.25),
            "FEW": BorrowQuote("easy", 0.01, available_shares=3.0),
        }
    )
    orders = [_o("sell", 5.0, t, "open") for t in ("NONE", "DEAR", "FEW", "OK")]
    prices = dict.fromkeys(("NONE", "DEAR", "FEW", "OK"), 10.0)
    result = _run(
        orders,
        Portfolio(cash=10_000.0),
        prices,
        _policy(borrow_check={"enabled": True, "max_borrow_fee": 0.10}),
        borrow=borrow,
    )
    assert [(o.ticker, o.quantity) for o in result.orders] == [("FEW", 3.0), ("OK", 5.0)]
    assert {a.ticker for a in result.adjustments} == {"NONE", "DEAR", "FEW"}


def test_borrow_check_without_a_source_allows_no_short() -> None:
    result = _run(
        [_o("sell", 5.0, effect="open"), _o("buy", 1.0, "B", "open")],
        Portfolio(cash=1_000.0),
        {"X": 10.0, "B": 10.0},
        _policy(borrow_check={"enabled": True}),
    )
    assert [o.ticker for o in result.orders] == ["B"]
    # Its own static source works when the caller has none.
    result = _run(
        [_o("sell", 5.0, effect="open")],
        Portfolio(cash=1_000.0),
        {"X": 10.0},
        _policy(borrow_check={"enabled": True, "borrow": {"hard": ["Y"]}}),
    )
    assert [o.quantity for o in result.orders] == [5.0]


# ---- squeeze guard ------------------------------------------------------------------------------


def test_squeeze_guard_covers_on_an_adverse_move_by_hand() -> None:
    # Short 100 entered at 50; now 65 is 30 % above entry, over 25 %.
    history = {"X": _bars([50.0] * 10 + [65.0])}
    entry = history["X"].index[-5].date()
    result = _run(
        [],
        Portfolio(cash=15_000.0, positions={"X": -100.0}),
        {"X": 65.0},
        _policy(squeeze_guard={"max_adverse_pct": 0.25}),
        history=history,
        entry_dates={"X": entry},
    )
    [cover] = result.orders
    assert cover.client_id == "2026-03-20:risk.squeeze_guard:X:cover"
    assert (cover.side, cover.quantity, cover.position_effect) == ("buy", 100.0, "close")


def test_squeeze_guard_atr_and_spike_triggers() -> None:
    # Flat at 50 then 60: ATR(20) is about 1 (the 2 % bar range) plus the
    # jump, so a 10-point rise is over 3 ATR. The same bars rose 20 % in
    # 5 bars, over a 15 % spike limit.
    history = {"X": _bars([50.0] * 30 + [60.0])}
    entry = history["X"].index[-10].date()
    book = Portfolio(cash=15_000.0, positions={"X": -100.0})
    atr = _run(
        [],
        book,
        {"X": 60.0},
        _policy(squeeze_guard={"atr_multiple": 3.0}),
        history=history,
        entry_dates={"X": entry},
    )
    assert [o.quantity for o in atr.orders] == [100.0]
    spike = _run(
        [_o("sell", 5.0, "X", "open")],
        book,
        {"X": 60.0},
        _policy(squeeze_guard={"spike_pct": 0.15}),
        history=history,
    )
    assert [(o.side, o.quantity) for o in spike.orders] == [("buy", 100.0)]


def test_squeeze_guard_on_borrow_fee_blocks_new_shorts_and_tops_up_covers() -> None:
    borrow = FlatBorrow(overrides={"X": BorrowQuote("hard", 0.5), "Y": BorrowQuote("hard", 0.5)})
    result = _run(
        [_o("buy", 40.0, effect="close"), _o("sell", 5.0, "Y", "open")],
        Portfolio(cash=15_000.0, positions={"X": -100.0}),
        {"X": 50.0, "Y": 10.0},
        _policy(squeeze_guard={"max_borrow_fee": 0.2}),
        borrow=borrow,
    )
    assert sorted((o.ticker, o.quantity) for o in result.orders) == [("X", 40.0), ("X", 60.0)]


def test_squeeze_guard_quiet_market_does_nothing() -> None:
    history = {"X": _bars([50.0] * 30)}
    orders = [_o("sell", 5.0, effect="open")]
    result = _run(
        orders,
        Portfolio(cash=15_000.0, positions={"X": -100.0}),
        {"X": 50.0},
        _policy(
            squeeze_guard={
                "max_adverse_pct": 0.25,
                "spike_pct": 0.3,
                "max_borrow_fee": 0.2,
                "borrow": {},
            }
        ),
        history=history,
        entry_dates={"X": history["X"].index[0].date()},
    )
    assert result.orders == orders


def test_squeeze_guard_without_history_or_entry_skips_price_triggers() -> None:
    result = _run(
        [],
        Portfolio(cash=15_000.0, positions={"X": -100.0}),
        {"X": 500.0},
        _policy(squeeze_guard={"max_adverse_pct": 0.1, "spike_pct": 0.1}),
    )
    assert result.orders == []


# ---- settings merge -----------------------------------------------------------------------------


def test_overrides_only_tighten_the_short_rules() -> None:
    base = RuleSettings.model_validate(
        {
            "gross_exposure": {"max_gross": 1.5},
            "net_exposure": {"min_net": -0.2, "max_net": 0.8},
            "short_caps": {"max_short_weight": 0.05},
            "margin_call": {"enabled": False, "buffer": 0.1},
        }
    )
    merged = tighter_rule_settings(
        base,
        {
            "gross_exposure": {"max_gross": 3.0},
            "net_exposure": {"min_net": 0.0, "max_net": 1.0},
            "short_caps": {"max_short_weight": 0.02, "max_short_total": 0.3},
            "margin_call": {"enabled": True, "buffer": 0.0, "margin": {"model": "cash"}},
            "borrow_check": {"enabled": True, "max_borrow_fee": 0.1},
            "squeeze_guard": {"spike_bars": 10, "max_adverse_pct": 0.2},
        },
    )
    assert merged.gross_exposure.max_gross == 1.5
    assert (merged.net_exposure.min_net, merged.net_exposure.max_net) == (0.0, 0.8)
    assert merged.short_caps.max_short_weight == 0.02
    assert merged.short_caps.max_short_total == 0.3
    assert merged.margin_call.enabled and merged.margin_call.buffer == 0.1
    assert merged.margin_call.margin == MarginSettings(model="reg_t")
    assert merged.borrow_check.enabled
    assert merged.squeeze_guard.spike_bars == 10


# ---- properties -------------------------------------------------------------------------------


def _gross(positions, prices) -> float:
    return sum(abs(q) * prices[t] for t, q in positions.items())


@pytest.mark.parametrize("seed", range(25))
def test_rules_never_raise_gross_and_never_block_a_close(seed: int) -> None:
    rng = random.Random(seed)
    tickers = ["A", "B", "C", "D"]
    prices = {t: rng.uniform(5, 200) for t in tickers}
    positions = {t: rng.choice([-1, 1]) * rng.uniform(1, 50) for t in tickers if rng.random() < 0.7}
    book = Portfolio(cash=rng.uniform(1_000, 20_000), positions=positions)
    orders = []
    for t in tickers:
        if rng.random() < 0.6:
            side = rng.choice(["buy", "sell"])
            orders.append(Order(f"2026-03-20:s:{t}:{side}", t, side, rng.uniform(1, 80)))  # type: ignore[arg-type]
    policy = _policy(
        margin_call={"enabled": True, "buffer": rng.uniform(0, 0.5)},
        gross_exposure={"max_gross": rng.uniform(0.2, 2.0)},
        net_exposure={"min_net": -rng.uniform(0, 1), "max_net": rng.uniform(0, 1)},
        short_caps={"max_short_weight": rng.uniform(0, 0.2), "max_short_total": rng.uniform(0, 1)},
        borrow_check={"enabled": True, "borrow": BorrowSettings(hard=("C",)).model_dump()},
    )
    result = _run(orders, book, prices, policy)

    from stonks.execution.orders import classify_all

    proposed = classify_all(orders, positions)

    def after(os):
        pos = dict(positions)
        for o in os:
            pos[o.ticker] = pos.get(o.ticker, 0.0) + (
                o.quantity if o.side == "buy" else -o.quantity
            )
        return pos

    raw_gross = _gross(after(proposed), prices)
    kept_gross = _gross(after(result.orders), prices)
    forced = [o for o in result.orders if o.client_id.startswith("2026-03-20:risk.")]
    if not forced:
        assert kept_gross <= max(raw_gross, _gross(positions, prices)) + 1e-6
    # Every proposed close survives in full, unless the margin call added to it.
    for close in (o for o in proposed if o.position_effect == "close"):
        kept = sum(
            o.quantity
            for o in result.orders
            if o.ticker == close.ticker and o.side == close.side and o.position_effect == "close"
        )
        assert kept >= close.quantity - 1e-9


# ---- BE-04: entry dates of shorts ------------------------------------------------------------


def test_be04_a_short_gets_an_entry_date() -> None:
    from stonks.production.risk import entry_dates_from_fills

    d1, d2 = date(2026, 3, 2), date(2026, 3, 3)
    assert entry_dates_from_fills([("X", "sell", 10.0, d1)]) == {"X": d1}
    # adding to a short keeps its first entry
    assert entry_dates_from_fills([("X", "sell", 10.0, d1), ("X", "sell", 5.0, d2)]) == {"X": d1}


def test_be04_a_flip_restarts_the_entry_and_flat_drops_it() -> None:
    from stonks.production.risk import entry_dates_from_fills

    d1, d2, d3 = date(2026, 3, 2), date(2026, 3, 3), date(2026, 3, 4)
    flip = [("X", "buy", 10.0, d1), ("X", "sell", 20.0, d2)]
    assert entry_dates_from_fills(flip) == {"X": d2}
    back = [*flip, ("X", "buy", 30.0, d3)]
    assert entry_dates_from_fills(back) == {"X": d3}
    assert entry_dates_from_fills([*flip, ("X", "buy", 10.0, d3)]) == {}


def test_be04_max_holding_covers_an_old_short() -> None:
    history = {"X": _bars([50.0] * 40)}
    entry = history["X"].index[-30].date()
    result = _run(
        [_o("sell", 5, effect="open")],
        Portfolio(cash=15_000.0, positions={"X": -100.0}),
        {"X": 50.0},
        _policy(max_holding={"max_holding_bars": 20}),
        history=history,
        entry_dates={"X": entry},
    )
    [cover] = result.orders
    assert cover.client_id == "2026-03-20:risk.max_holding:X:cover"
    assert (cover.side, cover.quantity, cover.position_effect) == ("buy", 100.0, "close")
