"""RuleStrategy: a declarative spec driving estimate_return + decide."""

from __future__ import annotations

import copy
import json
from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from stonks.core.protocols import Strategy
from stonks.core.types import Portfolio
from stonks.strategies.rule_based import RULE_STRATEGY_CLASS_PATH, RuleStrategy
from stonks.strategies.rules import TEMPLATES, RuleSpecError
from stonks.strategies.rules.sample import SampleLake, synthetic_bars


def _frame(closes, start="2026-01-01") -> pd.DataFrame:
    closes = np.asarray(closes, dtype=float)
    return pd.DataFrame(
        {
            "timestamp": pd.bdate_range(start, periods=len(closes)),
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "adj_close": closes,
            "volume": np.full(len(closes), 1000.0),
        }
    )


def _cmp(left, op, right):
    def operand(v):
        if isinstance(v, str):
            return {"type": "indicator", "id": v}
        return {"type": "constant", "value": v}

    return {"type": "compare", "left": operand(left), "op": op, "right": operand(right)}


def _spec(**overrides) -> dict:
    spec = {
        "version": 1,
        "name": "close above 10",
        "indicators": [
            {"id": "close", "kind": "close"},
            {"id": "roc", "kind": "roc", "period": 1},
        ],
        "entry": _cmp("close", ">", 10),
        "rank": {"by": "close"},
        "sizing": {"max_positions": 2, "allocation": 1.0},
    }
    spec.update(overrides)
    return spec


def _ts(frame, i) -> datetime:
    return frame["timestamp"].iloc[i].to_pydatetime()


def test_satisfies_strategy_protocol_and_class_path():
    s = RuleStrategy({"spec": _spec()})
    assert isinstance(s, Strategy)
    assert RULE_STRATEGY_CLASS_PATH == "stonks.strategies.rule_based:RuleStrategy"
    assert RuleStrategy._class_path() == RULE_STRATEGY_CLASS_PATH


def test_parameter_spec_is_a_single_untunable_spec_param():
    (param,) = RuleStrategy.parameter_spec()
    assert param.name == "spec"
    assert param.tunable is False
    RuleStrategy({})  # the default spec is valid


def test_invalid_spec_raises_with_path():
    bad = _spec(rank={"by": "missing"})
    with pytest.raises(RuleSpecError) as exc:
        RuleStrategy({"spec": bad})
    assert exc.value.issues[0].path == "rank.by"
    assert isinstance(exc.value, ValueError)


def test_spec_accepted_as_json_string_and_normalized():
    s = RuleStrategy({"spec": json.dumps(_spec())})
    assert isinstance(s.params["spec"], dict)
    assert s.params["spec"]["sizing"]["top_k"] is None  # defaults filled in


def test_applicable_asset_classes_follow_the_spec():
    s = RuleStrategy({"spec": _spec(universe={"asset_classes": ["crypto", "equity"]})})
    assert set(s.applicable_asset_classes) == {"crypto", "equity"}


def test_estimate_return_positive_only_when_entry_true():
    frame = _frame([9, 9, 11, 12])
    lake = SampleLake({"A": frame})
    s = RuleStrategy({"spec": _spec()})
    assert s.estimate_return("A", _ts(frame, 1), lake) is None
    r = s.estimate_return("A", _ts(frame, 2), lake)
    assert r is not None and r > 0
    assert s.estimate_return("A", _ts(frame, 3), lake) > r  # desc rank by close
    assert s.estimate_return("NOPE", _ts(frame, 3), lake) is None
    assert s.estimate_return("A", _ts(frame, 3), None) is None


def test_ascending_rank_reverses_order():
    lake = SampleLake({"A": _frame([11, 11]), "B": _frame([20, 20])})
    s = RuleStrategy({"spec": _spec(rank={"by": "close", "order": "asc"})})
    as_of = _ts(_frame([1, 1]), 1)
    assert s.estimate_return("A", as_of, lake) > s.estimate_return("B", as_of, lake)


def test_rank_score_positive_for_negative_indicators():
    frame = _frame([20, 15])  # roc = -25%
    lake = SampleLake({"A": frame})
    s = RuleStrategy({"spec": _spec(rank={"by": "roc"})})
    r = s.estimate_return("A", _ts(frame, 1), lake)
    assert r is not None and r > 0


def test_estimate_return_is_look_ahead_safe():
    frame = _frame([9, 9, 9, 50])
    s = RuleStrategy({"spec": _spec()})
    lake = SampleLake({"A": frame})
    # the future bar (50) must not flip the signal at bar 2
    assert s.estimate_return("A", _ts(frame, 2), lake) is None
    truncated = SampleLake({"A": frame.iloc[:3]})
    assert RuleStrategy({"spec": _spec()}).estimate_return("A", _ts(frame, 2), truncated) is None


def test_exit_condition_suppresses_entry_signal():
    frame = _frame([11, 12, 30])
    spec = _spec(exit=_cmp("close", ">", 25))
    s = RuleStrategy({"spec": spec})
    lake = SampleLake({"A": frame})
    assert s.estimate_return("A", _ts(frame, 1), lake) is not None
    assert s.estimate_return("A", _ts(frame, 2), lake) is None


def test_asset_class_and_ticker_filters():
    frame = _frame([11, 12])
    lake = SampleLake({"A": frame, "B": frame, "C": frame}, asset_classes={"B": "crypto"})
    as_of = _ts(frame, 1)
    s = RuleStrategy({"spec": _spec()})
    assert s.estimate_return("A", as_of, lake) is not None  # unknown class → equity
    assert s.estimate_return("B", as_of, lake) is None
    only_c = RuleStrategy({"spec": _spec(universe={"asset_classes": ["equity"], "tickers": ["C"]})})
    assert only_c.estimate_return("A", as_of, lake) is None
    assert only_c.estimate_return("C", as_of, lake) is not None


# ---- decide -----------------------------------------------------------------

AS_OF = datetime(2026, 3, 2)


def _prices(**p):
    return {k: float(v) for k, v in p.items()}


def test_decide_buys_top_ranked_equal_weight_within_cash():
    s = RuleStrategy({"spec": _spec(sizing={"max_positions": 4, "allocation": 1.0})})
    picks = [(3.0, "A"), (2.0, "B"), (1.0, "C")]
    orders = s.decide(picks, Portfolio(cash=1000.0), _prices(A=10, B=20, C=50), AS_OF)
    assert [(o.side, o.ticker) for o in orders] == [("buy", "A"), ("buy", "B"), ("buy", "C")]
    spend = {o.ticker: o.quantity * _prices(A=10, B=20, C=50)[o.ticker] for o in orders}
    assert spend == pytest.approx({"A": 250.0, "B": 250.0, "C": 250.0})


def test_decide_respects_max_positions_and_top_k():
    s = RuleStrategy({"spec": _spec(sizing={"max_positions": 2, "allocation": 1.0})})
    port = Portfolio(cash=1000.0, positions={"H": 1.0})
    picks = [(3.0, "A"), (2.0, "B"), (1.0, "H")]
    orders = s.decide(picks, port, _prices(A=10, B=10, H=10), AS_OF)
    assert [o.ticker for o in orders] == ["A"]  # one free slot, H already held
    s2 = RuleStrategy({"spec": _spec(sizing={"max_positions": 5, "top_k": 1, "allocation": 1.0})})
    orders = s2.decide(picks, Portfolio(cash=1000.0), _prices(A=10, B=10, H=10), AS_OF)
    assert [o.ticker for o in orders] == ["A"]


def test_decide_never_spends_more_than_cash():
    s = RuleStrategy({"spec": _spec(sizing={"max_positions": 2, "allocation": 1.0})})
    # equity is dominated by the held position, so the per-slot budget
    # (5_050 / 2) far exceeds the 50 of cash
    port = Portfolio(cash=50.0, positions={"H": 100.0})
    orders = s.decide([(1.0, "A"), (0.5, "H")], port, _prices(A=10, H=50), AS_OF)
    total = sum(o.quantity * 10 for o in orders if o.side == "buy")
    assert total <= 50.0 + 1e-9
    assert [o.ticker for o in orders] == ["A"]
    assert s.decide([(1.0, "A")], Portfolio(cash=0.0), _prices(A=10), AS_OF) == []


def test_decide_skips_unpriced_picks():
    s = RuleStrategy({"spec": _spec()})
    orders = s.decide([(2.0, "A"), (1.0, "B")], Portfolio(cash=100.0), _prices(B=10), AS_OF)
    assert [o.ticker for o in orders] == ["B"]


def test_exit_condition_sells_before_buys():
    frame = _frame([11, 30])
    lake = SampleLake({"H": frame, "A": _frame([12, 13])})
    s = RuleStrategy({"spec": _spec(exit=_cmp("close", ">", 25))})
    as_of = _ts(frame, 1)
    assert s.estimate_return("H", as_of, lake) is None  # exit is true for H
    r_a = s.estimate_return("A", as_of, lake)
    port = Portfolio(cash=100.0, positions={"H": 2.0})
    orders = s.decide([(r_a, "A")], port, _prices(H=30, A=13), as_of)
    assert [(o.side, o.ticker) for o in orders] == [("sell", "H"), ("buy", "A")]
    assert orders[0].quantity == 2.0


def test_held_ticker_kept_when_entry_false_unless_configured():
    frame = _frame([11, 9])
    lake = SampleLake({"H": frame})
    as_of = _ts(frame, 1)
    port = Portfolio(cash=0.0, positions={"H": 1.0})
    keep = RuleStrategy({"spec": _spec()})
    assert keep.estimate_return("H", as_of, lake) is None
    assert keep.decide([], port, _prices(H=9), as_of) == []
    drop = RuleStrategy({"spec": _spec(exit_when_entry_false=True)})
    assert drop.estimate_return("H", as_of, lake) is None
    orders = drop.decide([], port, _prices(H=9), as_of)
    assert [(o.side, o.ticker) for o in orders] == [("sell", "H")]


def test_stop_loss_and_take_profit_on_closes():
    spec = _spec(risk={"stop_loss_pct": 0.1, "take_profit_pct": 0.5})
    s = RuleStrategy({"spec": spec})
    buy = s.decide([(1.0, "A"), (0.5, "B")], Portfolio(cash=200.0), _prices(A=100, B=100), AS_OF)
    assert {o.ticker for o in buy} == {"A", "B"}
    held = Portfolio(cash=0.0, positions={"A": 1.0, "B": 1.0})
    later = datetime(2026, 3, 3)
    # A fell 10% (stop), B is up 20% (no exit yet)
    orders = s.decide([(1.0, "A"), (0.5, "B")], held, _prices(A=90, B=120), later)
    assert [(o.side, o.ticker) for o in orders] == [("sell", "A")]
    orders = s.decide([(0.5, "B")], Portfolio(0.0, {"B": 1.0}), _prices(B=150), later)
    assert [(o.side, o.ticker) for o in orders] == [("sell", "B")]


def test_client_ids_are_unique_and_deterministic():
    s = RuleStrategy({"spec": _spec()})
    orders = s.decide([(2.0, "A"), (1.0, "B")], Portfolio(cash=100.0), _prices(A=1, B=1), AS_OF)
    ids = [o.client_id for o in orders]
    assert len(set(ids)) == 2
    assert all(o.strategy_id == RuleStrategy.id for o in orders)


# ---- persistence ------------------------------------------------------------


def test_save_load_round_trip(tmp_path):
    spec = copy.deepcopy(TEMPLATES["rsi_mean_reversion"].spec)
    s = RuleStrategy({"spec": spec})
    s.save(tmp_path / "a")
    meta = json.loads((tmp_path / "a" / "meta.json").read_text())
    assert meta["class_path"] == RULE_STRATEGY_CLASS_PATH
    loaded = RuleStrategy.load(tmp_path / "a")
    assert loaded.params == s.params
    assert loaded.spec == s.spec


def test_bound_class_carries_spec_as_default_but_saves_as_rule_strategy(tmp_path):
    spec = TEMPLATES["sma_trend_following"].spec
    bound = RuleStrategy.bind(spec)
    assert issubclass(bound, RuleStrategy)
    inst = bound({})
    assert inst.spec.name == "SMA trend following"
    assert bound._class_path() == RULE_STRATEGY_CLASS_PATH
    inst.save(tmp_path / "b")
    assert RuleStrategy.load(tmp_path / "b").spec == inst.spec


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_templates_run_on_sample_data(name):
    lake = SampleLake({"S1": synthetic_bars(300, seed=1), "S2": synthetic_bars(300, seed=2)})
    s = RuleStrategy({"spec": TEMPLATES[name].spec})
    stamps = synthetic_bars(300, seed=1)["timestamp"]
    for ts in stamps.iloc[-60:]:
        for ticker in ("S1", "S2"):
            r = s.estimate_return(ticker, ts.to_pydatetime(), lake)
            assert r is None or r > 0
