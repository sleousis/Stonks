"""The style exposure rule (roadmap 22.4): opening orders are scaled so the
book's net exposure to each style stays inside a cap. Off by default, and
overrides can only tighten it."""

from __future__ import annotations

import pandas as pd
import pytest

from stonks.core.types import Portfolio
from stonks.production.rules import registered_rules
from stonks.production.rules.settings import RuleSettings, tighter_rule_settings
from stonks.production.rules.style_exposure import StyleExposureSettings, bars_style_exposures
from tests.fixtures.risk_rules import bars, buy, context, policy, sell

NAMES = ["A.US", "B.US", "C.US", "D.US", "E.US"]
PRICES = dict.fromkeys(NAMES, 100.0)
#: Raw momentum 1..5: standardised, A is -1.41 and E is +1.41.
MOMENTUM = pd.DataFrame({"momentum": [1.0, 2.0, 3.0, 4.0, 5.0]}, index=NAMES)
Z_E = 2 / (2**0.5)


def _rule():
    [rule] = [r for r in registered_rules() if r.name == "style_exposure"]
    return rule


def _ctx(portfolio: Portfolio, cap: float | None, exposures=MOMENTUM, **kw):
    rules = {} if cap is None else {"style_exposure": {"max_abs_exposure": cap}}
    return context(portfolio, PRICES, policy(**rules), factor_exposures=exposures, **kw)


def test_off_by_default():
    assert not _rule().enabled(policy())
    assert _rule().enabled(policy(style_exposure={"max_abs_exposure": 0.5}))
    assert _rule().needs_history


def test_a_buy_inside_the_cap_passes():
    ctx = _ctx(Portfolio(cash=100_000.0), cap=1.0)
    kept, adj = _rule().apply([buy("E.US", 500)], ctx)  # 50% in E: exposure 0.71
    assert kept[0].quantity == 500 and adj == []


def test_a_buy_over_the_cap_is_scaled():
    ctx = _ctx(Portfolio(cash=100_000.0), cap=0.5)
    kept, adj = _rule().apply([buy("E.US", 1_000)], ctx)  # 100% in E: 1.41
    assert kept[0].quantity == pytest.approx(1_000 * 0.5 / Z_E, rel=1e-6)
    assert adj[0].rule == "style_exposure"
    assert "momentum" in adj[0].reason


def test_offsetting_buys_pass():
    ctx = _ctx(Portfolio(cash=100_000.0), cap=0.2)
    kept, adj = _rule().apply([buy("A.US", 400), buy("E.US", 400)], ctx)
    assert [o.quantity for o in kept] == [400, 400] and adj == []


def test_sells_are_never_touched():
    held = Portfolio(cash=0.0, positions={"E.US": 1_000.0})
    ctx = _ctx(held, cap=0.1)
    kept, adj = _rule().apply([sell("E.US", 300)], ctx)
    assert kept[0].quantity == 300 and adj == []


def test_a_breaching_book_may_still_trade_towards_the_cap():
    held = Portfolio(cash=50_000.0, positions={"E.US": 500.0})  # exposure 0.71
    ctx = _ctx(held, cap=0.3)
    kept, _ = _rule().apply([buy("A.US", 300)], ctx)  # pulls exposure down
    assert kept[0].quantity == 300
    kept, adj = _rule().apply([buy("D.US", 300)], ctx)  # pushes it further out
    assert kept == [] and adj[0].adjusted_quantity == 0.0


def test_only_the_configured_styles_count():
    raw = MOMENTUM.assign(size=[5.0, 4.0, 3.0, 2.0, 1.0])
    rules = {"style_exposure": {"max_abs_exposure": 0.5, "styles": ["size"]}}
    ctx = context(Portfolio(cash=100_000.0), PRICES, policy(**rules), factor_exposures=raw)
    kept, adj = _rule().apply([buy("C.US", 1_000)], ctx)  # size 0 for C
    assert kept[0].quantity == 1_000 and adj == []
    kept, adj = _rule().apply([buy("A.US", 1_000)], ctx)
    assert kept[0].quantity < 1_000 and "size" in adj[0].reason


def test_without_given_exposures_the_bars_are_used():
    closes = {t: [100.0 * (1 + 0.001 * i * k) for k in range(300)] for i, t in enumerate(NAMES)}
    history = {t: bars(c, volume=1_000.0) for t, c in closes.items()}
    raw = bars_style_exposures(history, {})
    assert raw["momentum"].is_monotonic_increasing
    assert {"momentum", "size", "volatility"} <= set(raw.columns)
    ctx = _ctx(Portfolio(cash=100_000.0), cap=0.5, exposures=None, history=history)
    kept, adj = _rule().apply([buy("E.US", 1_000)], ctx)
    assert kept[0].quantity < 1_000 and adj[0].rule == "style_exposure"


def test_bars_exposures_read_no_bar_after_the_decision():
    closes = [100.0 + k for k in range(300)]
    history = {t: bars(closes) for t in NAMES[:2]}
    cutoff = history["A.US"].index[250]
    later = {
        t: f.assign(close=f["close"].where(f.index <= cutoff, 1.0)) for t, f in history.items()
    }
    base = bars_style_exposures(history, {}, as_of=cutoff.date())
    assert base.equals(bars_style_exposures(later, {}, as_of=cutoff.date()))


def test_settings_only_tighten():
    base = RuleSettings.model_validate({"style_exposure": {"max_abs_exposure": 0.5}})
    looser = tighter_rule_settings(base, {"style_exposure": {"max_abs_exposure": 0.9}})
    assert looser.style_exposure.max_abs_exposure == 0.5
    tighter = tighter_rule_settings(base, {"style_exposure": {"max_abs_exposure": 0.2}})
    assert tighter.style_exposure.max_abs_exposure == 0.2
    fewer = RuleSettings.model_validate({"style_exposure": {"styles": ["size"]}})
    merged = tighter_rule_settings(fewer, {"style_exposure": {"styles": ["value"]}})
    assert merged.style_exposure.styles == ("size", "value")
    with pytest.raises(ValueError):
        StyleExposureSettings(styles=("beauty",))  # type: ignore[arg-type]
