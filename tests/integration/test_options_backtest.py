"""The options backtest end to end (roadmap 17.3, 17.5): synthetic chains
ingested into a lake, read back through ``OptionMarketData.from_lake``,
and every catalogued options strategy run over them. Also the ledger
identity (equity = cash + marked positions), fills on the next day's
quotes (P12), expiry, splits, dividends and the risk rules in the loop.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import pandas as pd
import pytest

from stonks.backtest.options_engine import (
    OptionMarketData,
    OptionsBacktestConfig,
    OptionsBacktester,
)
from stonks.core.corporate_actions import CorporateActions, Dividend, Split
from stonks.core.params import ParameterSpec
from stonks.options.chain import OptionQuote, snapshots
from stonks.options.ingest import ingest_option_quotes
from stonks.options.orders import ComboLeg, ComboOrder
from stonks.options.store import OptionStore
from stonks.options.strategies import option_strategy_catalog, resolve_option_strategy
from stonks.options.strategy import OptionDecisionContext, OptionIntent, OptionStrategy
from stonks.options.synthetic import SyntheticChainSpec, SyntheticOptionSource
from stonks.production.rules.settings import RuleSettings

START = date(2025, 1, 2)
STRIKES = [float(k) for k in range(60, 165, 5)]


def business_days(n: int, start: date = START) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def path(days: list[date], start: float = 100.0, drift: float = 0.0008, wiggle: float = 0.01):
    return {
        d: round(start * math.exp(drift * i + wiggle * math.sin(i / 3.0)), 2)
        for i, d in enumerate(days)
    }


def market(closes: dict[date, float], **spec) -> OptionMarketData:
    src = SyntheticOptionSource(
        {"X.US": closes},
        SyntheticChainSpec(**{"vol": 0.25, "horizon_days": 70, **spec}),
        fixed_strikes={"X.US": STRIKES},
    )
    rows = list(src.fetch_option_quotes("X.US"))
    quotes = [
        OptionQuote(
            r.contract,
            r.as_of,
            r.bid,
            r.ask,
            r.last,
            r.volume,
            r.open_interest,
            r.underlying_price,
            r.iv,
            r.delta,
        )
        for r in rows
    ]
    return OptionMarketData(closes={"X.US": closes}, chains=snapshots(quotes))


@pytest.fixture(scope="module")
def data() -> OptionMarketData:
    days = business_days(160)
    return market(path(days))


def config(**kw) -> OptionsBacktestConfig:
    base = {
        "start": START,
        "end": START + timedelta(days=400),
        "underlyings": ["X.US"],
        "initial_cash": 100_000.0,
    }
    return OptionsBacktestConfig(**{**base, **kw})


def test_catalog_lists_the_five_strategies_with_hypotheses():
    catalog = option_strategy_catalog()
    assert set(catalog) == {
        "covered_call",
        "cash_secured_put",
        "protective_put",
        "vertical_spread",
        "vol_premium_condor",
    }
    for cls in catalog.values():
        assert len(cls.hypothesis) > 80, cls.id
        assert cls.structures, cls.id
        cls()  # defaults validate
    with pytest.raises(ValueError):
        resolve_option_strategy("nope")


@pytest.mark.parametrize("sid", sorted(option_strategy_catalog()))
def test_every_strategy_runs_and_trades(data, sid):
    cls = resolve_option_strategy(sid)
    params = {"lookback": 21} if sid == "vertical_spread" else {}
    if sid == "protective_put":
        params = {"trend_days": 20}
    if sid == "vol_premium_condor":
        params = {"iv_ratio": 0.8}
    result = OptionsBacktester(cls(params), data, config()).run()
    assert result.report.n_bars == 160
    assert result.n_fills > 0, sid
    assert all(math.isfinite(v) for v in result.report.equity_curve)
    kinds = {e.kind for e in result.events}
    assert "fill" in kinds or "share_fill" in kinds


def test_covered_call_writes_calls_on_held_shares_and_they_expire_or_roll(data):
    result = OptionsBacktester(resolve_option_strategy("covered_call")(), data, config()).run()
    kinds = [e.kind for e in result.events]
    assert kinds[0] == "share_fill"
    assert kinds.count("fill") >= 2
    # a call is never written on more shares than held
    for fill in result.fills:
        for leg in fill.legs:
            if ":C:" in leg.instrument:
                assert leg.quantity >= -result.ledger.shares.get("X.US", 1_000) / 100 - 10


def test_fills_happen_the_day_after_the_decision(data):
    class Once(OptionStrategy):
        id = "once"
        structures = ("long_call",)

        @classmethod
        def parameter_spec(cls):
            return [ParameterSpec("x", "int", 0, (0, 1))]

        def decide(self, ctx: OptionDecisionContext):
            if ctx.as_of == START:
                return [OptionIntent("X.US", "long_call", {"delta": 0.5, "dte": 30})]
            return []

    result = OptionsBacktester(Once(), data, config()).run()
    assert len(result.fills) == 1
    assert result.fills[0].as_of == business_days(2)[1]
    assert result.fills[0].client_id.startswith("once:2025-01-02:X.US:long_call")


def test_long_call_held_to_expiry_is_exercised():
    days = business_days(40)
    closes = {d: 100.0 + i for i, d in enumerate(days)}  # rallies through the strike
    data = market(closes)

    class Hold(OptionStrategy):
        id = "hold"
        structures = ("long_call",)

        @classmethod
        def parameter_spec(cls):
            return []

        def decide(self, ctx):
            if ctx.as_of == START:
                return [OptionIntent("X.US", "long_call", {"delta": 0.5, "dte": 16})]
            return []

    result = OptionsBacktester(Hold(), data, config()).run()
    kinds = [e.kind for e in result.events]
    assert "exercise" in kinds
    assert result.ledger.shares.get("X.US") == 100
    assert result.ledger.options == {}


def test_ledger_identity_every_day(data):
    """Equity on the curve equals cash plus marked positions: re-deriving the
    last point from the final ledger and the last day's marks."""
    result = OptionsBacktester(
        resolve_option_strategy("vertical_spread")({"lookback": 21}), data, config()
    ).run()
    led = result.ledger
    last_day = result.report.equity_dates[-1]
    chain = data.chains[("X.US", last_day)]
    marks = {cid: chain.by_id()[cid].mark for cid in led.options if cid in chain.by_id()}
    assert len(marks) == len(led.options)
    value = led.value({"X.US": data.closes["X.US"][last_day]}, marks)
    assert value == pytest.approx(result.report.equity_curve[-1])


def test_missing_quote_days_and_wider_fills_cost_money(data):
    cls = resolve_option_strategy("vol_premium_condor")
    base = OptionsBacktester(cls({"iv_ratio": 0.8}), data, config()).run()
    wide = OptionsBacktester(
        cls({"iv_ratio": 0.8}),
        data,
        config(fills=config().fills.model_copy(update={"spread_fraction": 1.5})),
    ).run()
    assert wide.report.equity_curve[-1] < base.report.equity_curve[-1]
    gaps = OptionsBacktester(cls({"iv_ratio": 0.8}), data, config(drop_quote_days=0.3)).run()
    assert gaps.report.n_bars == base.report.n_bars
    assert gaps.n_fills <= base.n_fills


def test_split_and_dividend_are_applied_to_the_book():
    days = business_days(30)
    closes = dict.fromkeys(days, 100.0)
    split_day = days[10]
    for d in days[10:]:
        closes[d] = 50.0
    data = market(closes)
    actions = CorporateActions.from_events(
        [Split("X.US", split_day, 2.0), Dividend("X.US", days[5], 0.5)]
    )
    data = OptionMarketData(closes=data.closes, chains=data.chains, actions=actions)

    class Buy(OptionStrategy):
        id = "buy"

        @classmethod
        def parameter_spec(cls):
            return []

        def decide(self, ctx):
            if ctx.as_of == START:
                call = max(
                    (
                        q.contract
                        for q in ctx.chains["X.US"].filter(right="call", two_sided=True)
                        if q.contract.strike == 110
                    ),
                    key=lambda c: c.expiry,
                )
                return [
                    ComboOrder(
                        "bw",
                        (ComboLeg("buy", 100, shares="X.US"), ComboLeg("buy", 1, call)),
                    )
                ]
            return []

    result = OptionsBacktester(Buy(), data, config()).run()
    kinds = [e.kind for e in result.events]
    assert "dividend" in kinds and "split" in kinds, result.rejections
    assert result.ledger.shares["X.US"] == 200
    # the 110 call became two 55 calls
    assert list(result.ledger.options.values()) == [2.0]
    assert next(iter(result.ledger.options)).endswith(":C:55")
    div = next(e for e in result.events if e.kind == "dividend")
    assert div.cash_delta == pytest.approx(50.0)
    # equity does not jump on the split day: 100 x 100 before, 200 x 50 after
    i = result.report.equity_dates.index(split_day)
    assert result.report.equity_curve[i] == pytest.approx(
        result.report.equity_curve[i - 1], rel=0.02
    )


def test_risk_rules_drop_whole_combos_in_the_loop(data):
    cls = resolve_option_strategy("vol_premium_condor")
    rules = RuleSettings.model_validate({"option_max_loss": {"max_loss_per_group": 0.0001}})
    result = OptionsBacktester(cls({"iv_ratio": 0.8}), data, config(rules=rules)).run()
    assert result.n_fills == 0
    assert result.risk_adjustments
    assert {a.rule for a in result.risk_adjustments} == {"option_max_loss"}
    # a generous limit lets them through
    loose = RuleSettings.model_validate(
        {
            "option_max_loss": {"max_loss_per_group": 0.5},
            "option_margin": {"enabled": True},
            "short_option_guard": {"enabled": True, "approval_level": 3},
            "option_greek_limits": {"max_dollar_delta": 5.0},
        }
    )
    ok = OptionsBacktester(cls({"iv_ratio": 0.8}), data, config(rules=loose)).run()
    assert ok.n_fills > 0


def test_short_option_guard_blocks_spreads_below_level_three(data):
    cls = resolve_option_strategy("vol_premium_condor")
    rules = RuleSettings.model_validate({"short_option_guard": {"enabled": True}})
    result = OptionsBacktester(cls({"iv_ratio": 0.8}), data, config(rules=rules)).run()
    assert result.n_fills == 0
    assert all("level 3" in a.reason for a in result.risk_adjustments)


def test_from_lake_reads_what_the_ingest_wrote(lake):
    days = business_days(25)
    closes = path(days)
    lake.upsert_prices(
        pd.DataFrame(
            [
                {
                    "ticker": "X.US",
                    "date": d,
                    "open": c,
                    "high": c,
                    "low": c,
                    "close": c,
                    "adj_close": c,
                    "volume": 1e6,
                }
                for d, c in closes.items()
            ]
        )
    )
    src = SyntheticOptionSource(
        {"X.US": closes}, SyntheticChainSpec(horizon_days=40), fixed_strikes={"X.US": STRIKES}
    )
    ingest_option_quotes(src, lake, ["X.US"])
    data = OptionMarketData.from_lake(lake, ["X.US"], days[0], days[-1])
    assert len(data.chains) == 25
    assert data.closes["X.US"][days[3]] == closes[days[3]]
    assert OptionStore(lake).quote_days("X.US") == days
    result = OptionsBacktester(
        resolve_option_strategy("cash_secured_put")(),
        data,
        config(start=days[0], end=days[-1]),
    ).run()
    assert result.n_fills >= 1


def test_validation_runs_the_applicable_survival_tests(data):
    from stonks.options.validation import OptionValidationSettings, validate_option_strategy

    cls = resolve_option_strategy("covered_call")
    reports = validate_option_strategy(
        cls, None, data, config(), OptionValidationSettings(min_fills=1, n_trials=5)
    )
    assert [r.test_id for r in reports] == [
        "oos",
        "deflated_sharpe",
        "fill_stress",
        "missing_quotes",
        "cost_stress",
    ]
    oos, dsr_ = reports[0], reports[1]
    assert 0.0 <= oos.metrics["psr"] <= 1.0
    assert dsr_.metrics["dsr"] <= oos.metrics["psr"] + 1e-12  # deflation never helps
    assert all(isinstance(r.passed, bool) for r in reports)
    # a window with no bars fails cleanly
    empty = validate_option_strategy(
        cls, None, data, config(start=date(2030, 1, 1), end=date(2030, 2, 1))
    )
    assert not empty[0].passed and empty[0].metrics["psr"] == 0.0
