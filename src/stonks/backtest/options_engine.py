"""The options backtest (roadmap 17.3, design section 4).

Runs one :class:`~stonks.options.strategy.OptionStrategy` over daily
underlying closes and end-of-day option chains. It is a separate engine
from ``backtest/engine.py`` so the stock backtest and every golden result
stay exactly as they were.

Each day, in order:

0. corporate actions going ex that day: a split multiplies shares and turns
   every option on the ticker into its adjusted contract (OCC rules,
   ``core.options.adjust_for_split``) and drops queued combos on it; a cash
   dividend is credited on held shares (a short pays it);
1. combos decided the day before fill against today's quotes, all legs or
   none (``OptionsSimulatedBroker``), so a decision never trades on the
   quotes it saw (P12);
2. short American options are checked for early assignment
   (``AssignmentModel``); risk flags are recorded either way;
3. options expiring today are exercised, assigned, cash-settled or expire
   worthless at today's close (OCC exercise by exception at 0.01);
4. the book is marked: an option at its quote's mark, else the model at
   its last known vol, else its intrinsic value;
5. the strategy decides; intents become combos through the structure
   registry; when ``rules`` is set every enabled option risk rule filters
   them (whole combos only) and the survivors queue for tomorrow.

The result carries a standard :class:`~stonks.backtest.report.
BacktestReport` of the equity curve, so the metrics and the statistical
survival tests read it like any other backtest, plus the fills, rejected
combos, ledger events and risk adjustments.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Any

from stonks.backtest.options_broker import (
    ComboFill,
    OptionFillSettings,
    OptionsSimulatedBroker,
    Rejection,
)
from stonks.backtest.options_ledger import LedgerEvent, OptionLedger
from stonks.backtest.report import BacktestReport, compute_report
from stonks.core.corporate_actions import CorporateActions, Dividend, Split
from stonks.logging import get_logger
from stonks.options.analytics import analyze, model_mark, risk_view
from stonks.options.assignment import AssignmentModel, ExtrinsicAssignmentModel
from stonks.options.chain import ChainSnapshot, OptionQuote
from stonks.options.orders import ComboOrder
from stonks.options.selector import LegSelector
from stonks.options.strategy import OptionDecisionContext, OptionIntent, OptionStrategy
from stonks.options.structures import BuildRequest, build

if TYPE_CHECKING:  # pragma: no cover
    from stonks.production.rules import RiskAdjustment
    from stonks.production.rules.settings import RuleSettings
    from stonks.store.lake import DuckDBLake

_log = get_logger("stonks.backtest.options_engine")


@dataclass(frozen=True)
class OptionMarketData:
    """What an options backtest reads: raw closes per underlying (fills,
    marks, exercise), the closes strategies see (split and dividend
    adjusted when the source has them, P13), chain snapshots by
    ``(underlying, day)``, and corporate actions."""

    closes: Mapping[str, Mapping[date, float]]
    chains: Mapping[tuple[str, date], ChainSnapshot] = field(
        default_factory=dict[tuple[str, date], ChainSnapshot]
    )
    actions: CorporateActions = field(default_factory=CorporateActions)
    adjusted: Mapping[str, Mapping[date, float]] | None = None

    @classmethod
    def from_lake(
        cls,
        lake: DuckDBLake,
        underlyings: Sequence[str],
        start: date,
        end: date,
        *,
        source: str | None = None,
        warmup_days: int = 400,
    ) -> OptionMarketData:
        from datetime import timedelta

        from stonks.options.chain import snapshots
        from stonks.options.store import OptionStore
        from stonks.store.corporate_actions import LakeCorporateActions

        closes: dict[str, dict[date, float]] = {}
        adjusted: dict[str, dict[date, float]] = {}
        for ticker in underlyings:
            frame = lake.get_prices(ticker, start - timedelta(days=warmup_days), end)
            closes[ticker] = {
                d: float(c) for d, c in zip(frame["date"], frame["close"], strict=True)
            }
            adj = frame["adj_close"].fillna(frame["close"])
            adjusted[ticker] = {d: float(c) for d, c in zip(frame["date"], adj, strict=True)}
        store = OptionStore(lake)
        quotes: list[OptionQuote] = []
        for ticker in underlyings:
            quotes.extend(store.quotes(ticker, start, end, source=source))
        return cls(
            closes=closes,
            chains=snapshots(quotes),
            actions=LakeCorporateActions(lake).load(list(underlyings)),
            adjusted=adjusted,
        )


@dataclass(frozen=True)
class OptionsBacktestConfig:
    start: date
    end: date
    underlyings: Sequence[str]
    initial_cash: float = 100_000.0
    fills: OptionFillSettings = field(default_factory=OptionFillSettings)
    selector: LegSelector = field(default_factory=LegSelector)
    #: Risk-free rate for model marks and Greeks (continuous, annual).
    rate: float = 0.0
    #: Option risk rules; ``None`` runs none.
    rules: RuleSettings | None = None
    dividend_withholding_rate: float = 0.0
    #: Drop this share of the days' chains (deterministic by day), to test
    #: that a result survives missing quotes. 0 keeps every chain.
    drop_quote_days: float = 0.0


@dataclass(frozen=True)
class OptionsBacktestResult:
    report: BacktestReport
    fills: tuple[ComboFill, ...]
    rejections: tuple[Rejection, ...]
    events: tuple[LedgerEvent, ...]
    risk_adjustments: tuple[RiskAdjustment, ...]
    ledger: OptionLedger

    @property
    def n_fills(self) -> int:
        return len(self.fills)


@dataclass(frozen=True)
class _RiskPolicy:
    """The piece of ``RiskPolicy`` the option rules read."""

    rules: Any


def _dropped(day: date, share: float) -> bool:
    if share <= 0:
        return False
    # a fixed hash of the day, the same on every run and every machine
    return (day.toordinal() * 2654435761 % 2**32) / 2**32 < share


class OptionsBacktester:
    def __init__(
        self,
        strategy: OptionStrategy,
        data: OptionMarketData,
        config: OptionsBacktestConfig,
        *,
        assignment: AssignmentModel | None = None,
    ) -> None:
        self._strategy = strategy
        self._data = data
        self._config = config
        self._assignment = assignment or ExtrinsicAssignmentModel()

    # ---- data access -------------------------------------------------------------

    def _days(self) -> list[date]:
        days: set[date] = set()
        for underlying in self._config.underlyings:
            days |= {
                d
                for d in self._data.closes.get(underlying, {})
                if self._config.start <= d <= self._config.end
            }
        return sorted(days)

    def _chain(self, underlying: str, day: date) -> ChainSnapshot | None:
        if _dropped(day, self._config.drop_quote_days):
            return None
        return self._data.chains.get((underlying, day))

    def _quotes(self, day: date) -> dict[str, OptionQuote]:
        out: dict[str, OptionQuote] = {}
        for underlying in self._config.underlyings:
            chain = self._chain(underlying, day)
            if chain is not None:
                out.update(chain.by_id())
        return out

    def _history(self, day: date) -> dict[str, list[tuple[date, float]]]:
        source = self._data.adjusted or self._data.closes
        return {
            u: sorted((d, c) for d, c in source.get(u, {}).items() if d <= day)
            for u in self._config.underlyings
        }

    def _next_dividends(self, day: date) -> dict[str, tuple[date, float]]:
        out: dict[str, tuple[date, float]] = {}
        for underlying in self._config.underlyings:
            for event in self._data.actions.for_ticker(underlying):
                if isinstance(event, Dividend) and event.ex_date > day:
                    out[underlying] = (event.ex_date, event.amount)
                    break
        return out

    # ---- the run -----------------------------------------------------------------

    def run(self) -> OptionsBacktestResult:
        cfg = self._config
        ledger = OptionLedger(cash=cfg.initial_cash)
        broker = OptionsSimulatedBroker(ledger, cfg.fills)
        spots: dict[str, float] = {}
        last_iv: dict[str, float] = {}
        last_mark: dict[str, float] = {}
        pending: list[ComboOrder] = []
        fills: list[ComboFill] = []
        rejections: list[Rejection] = []
        adjustments: list[RiskAdjustment] = []
        dates: list[date] = []
        curve: list[float] = []
        days = self._days()

        for i, day in enumerate(days):
            for underlying in cfg.underlyings:
                close = self._data.closes.get(underlying, {}).get(day)
                if close is not None:
                    spots[underlying] = close

            pending = self._corporate_actions(ledger, day, pending)

            quotes = self._quotes(day)
            for combo in pending:
                result = broker.place(combo, quotes, spots, day)
                (fills if isinstance(result, ComboFill) else rejections).append(result)
            pending = []

            marks = self._marks(ledger, quotes, spots, day, last_iv, last_mark)
            self._early_assignment(ledger, day, spots, marks)
            self._expire(ledger, day, spots)
            marks = {cid: m for cid, m in marks.items() if cid in ledger.options}

            equity = ledger.value(spots, marks)
            dates.append(day)
            curve.append(equity)

            if i == len(days) - 1:
                break
            decisions = self._decide(ledger, day, equity)
            combos = self._build(decisions, ledger, day, equity)
            if cfg.rules is not None and combos:
                combos, adj = self._risk(combos, ledger, day, spots, marks, last_iv)
                adjustments.extend(adj)
            pending = combos

        report = compute_report(self._strategy.id, dates, curve)
        return OptionsBacktestResult(
            report=report,
            fills=tuple(fills),
            rejections=tuple(rejections),
            events=tuple(ledger.events),
            risk_adjustments=tuple(adjustments),
            ledger=ledger,
        )

    # ---- steps ---------------------------------------------------------------------

    def _corporate_actions(
        self, ledger: OptionLedger, day: date, pending: list[ComboOrder]
    ) -> list[ComboOrder]:
        for underlying in self._config.underlyings:
            for event in self._data.actions.for_ticker(underlying):
                if event.ex_date != day:
                    continue
                if isinstance(event, Split):
                    ledger.apply_split(underlying, event.ratio, day)
                    kept = [c for c in pending if underlying not in c.underlyings]
                    if len(kept) != len(pending):
                        _log.info("options.split.dropped_pending", ticker=underlying, day=str(day))
                    pending = kept
                else:
                    ledger.apply_dividend(
                        underlying, event.amount, day, self._config.dividend_withholding_rate
                    )
        return pending

    def _marks(
        self,
        ledger: OptionLedger,
        quotes: Mapping[str, OptionQuote],
        spots: Mapping[str, float],
        day: date,
        last_iv: dict[str, float],
        last_mark: dict[str, float],
    ) -> dict[str, float]:
        marks: dict[str, float] = {}
        for cid in ledger.options:
            contract = ledger.contracts[cid]
            spot = spots.get(contract.underlying)
            quote = quotes.get(cid)
            if quote is not None:
                a = analyze(quote, spot, rate=self._config.rate)
                if a.iv is not None:
                    last_iv[cid] = a.iv
                if a.mark is not None:
                    marks[cid] = a.mark
                    last_mark[cid] = a.mark
                    continue
            if spot is not None:
                marks[cid] = model_mark(
                    contract, day, spot, last_iv.get(cid), rate=self._config.rate
                )
            elif cid in last_mark:
                marks[cid] = last_mark[cid]
        return marks

    def _early_assignment(
        self,
        ledger: OptionLedger,
        day: date,
        spots: Mapping[str, float],
        marks: Mapping[str, float],
    ) -> None:
        dividends = self._next_dividends(day)
        for cid in list(ledger.options):
            qty = ledger.options[cid]
            contract = ledger.contracts[cid]
            spot = spots.get(contract.underlying)
            if qty >= 0 or spot is None or contract.expiry <= day:
                continue
            check = self._assignment.check(
                contract, qty, day, spot, marks.get(cid), dividends.get(contract.underlying)
            )
            if check.flag:
                ledger.record(
                    LedgerEvent(day, "assignment_risk", cid, qty, spot, detail=check.reason)
                )
            if check.assign:
                ledger.settle(cid, spot, day, early=True)

    def _expire(self, ledger: OptionLedger, day: date, spots: Mapping[str, float]) -> None:
        for cid in list(ledger.options):
            contract = ledger.contracts[cid]
            if contract.expiry > day:
                continue
            spot = spots.get(contract.underlying)
            if spot is None:
                _log.warning("options.expiry.no_spot", contract=cid, day=str(day))
                continue
            ledger.settle(cid, spot, day)

    def _context(self, ledger: OptionLedger, day: date, equity: float) -> OptionDecisionContext:
        chains = {
            u: chain for u in self._config.underlyings if (chain := self._chain(u, day)) is not None
        }
        return OptionDecisionContext(
            as_of=day,
            history=self._history(day),
            chains=chains,
            ledger=ledger,
            equity=equity,
            next_dividends=self._next_dividends(day),
            rate=self._config.rate,
        )

    def _decide(self, ledger: OptionLedger, day: date, equity: float) -> list[Any]:
        return list(self._strategy.decide(self._context(ledger, day, equity)))

    def _build(
        self, decisions: Sequence[Any], ledger: OptionLedger, day: date, equity: float
    ) -> list[ComboOrder]:
        ctx = self._context(ledger, day, equity)
        combos: list[ComboOrder] = []
        for n, decision in enumerate(decisions):
            if isinstance(decision, ComboOrder):
                combos.append(decision)
                continue
            intent: OptionIntent = decision
            client_id = (
                f"{self._strategy.id}:{day.isoformat()}:{intent.underlying}:{intent.structure}:{n}"
            )
            combo = build(
                BuildRequest(intent, ctx, self._config.selector, client_id, self._strategy.id)
            )
            if combo is not None:
                combos.append(combo)
        return combos

    def _risk(
        self,
        combos: list[ComboOrder],
        ledger: OptionLedger,
        day: date,
        spots: Mapping[str, float],
        marks: Mapping[str, float],
        last_iv: Mapping[str, float],
    ) -> tuple[list[ComboOrder], list[RiskAdjustment]]:
        from stonks.production.rules import RiskContext, registered_rules
        from stonks.production.rules._options import OptionRiskRule

        chains = {
            u: chain for u in self._config.underlyings if (chain := self._chain(u, day)) is not None
        }
        view = risk_view(
            day,
            chains,
            spots,
            contracts=ledger.contracts,
            marks=marks,
            ivs=last_iv,
            groups=[g.legs for g in ledger.groups.values()],
            rate=self._config.rate,
            only=set(ledger.options)
            | {leg.instrument for combo in combos for leg in combo.option_legs},
        )
        portfolio, prices = ledger.portfolio(spots, dict(view.marks))
        for combo in combos:
            for leg in combo.option_legs:
                assert leg.contract is not None
                mark = view.marks.get(leg.contract.contract_id)
                if mark is not None:
                    prices.setdefault(leg.contract.contract_id, mark * leg.contract.multiplier)
        policy = _RiskPolicy(rules=self._config.rules)
        ctx = RiskContext(
            portfolio=portfolio,
            prices=prices,
            asset_classes={},
            policy=policy,  # type: ignore[arg-type]
            as_of=day,
            options=view,
        )
        orders = [o for combo in combos for o in combo.leg_orders()]
        adjustments: list[RiskAdjustment] = []
        for rule in registered_rules():
            if not isinstance(rule, OptionRiskRule) or not rule.enabled(policy):
                continue
            orders, adj = rule.apply(orders, ctx)
            adjustments.extend(adj)
        kept_ids = {(o.client_id, o.quantity) for o in orders}
        kept = [
            combo
            for combo in combos
            if all((o.client_id, o.quantity) in kept_ids for o in combo.leg_orders())
        ]
        return kept, adjustments
