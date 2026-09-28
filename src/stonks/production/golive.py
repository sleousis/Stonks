"""Go-live gate (roadmap 4.3), behind ``stonks golive check <id>``.

Evaluates a strategy's paper-trading period against ``GoLivePolicy``:

- ``status``: the strategy is ``shadow`` or ``active`` (a retired one has no
  paper period);
- ``min_days``: distinct days with a paper snapshot;
- ``max_drawdown``: deepest peak-to-trough fall within the period;
- ``max_drift``: |paper return - backtest-expected return| over the period,
  where the expectation compounds the latest ``oos`` survival report's
  ``cagr_oos`` over the period's calendar span;
- ``min_trades``: filled trades during the period;
- ``survival``: every stored survival report passed (when required).

The paper period is the strategy's virtual shadow P&L while it is
``shadow``, and the real (simulated or paper-broker) portfolio P&L once it
is ``active`` (the default portfolio's ledger only). The real portfolio is shared by all active strategies, so for
an active strategy the P&L checks describe the combined book; trades are
still counted per strategy. P&L is read only through
``stonks.production.pnl`` so its day grouping stays in one place.

Missing data never passes: no snapshots fails ``min_days``,
``max_drawdown`` and ``max_drift``; no backtest expectation fails
``max_drift``; no survival reports fails ``survival``.

Incubation grade (BL-25) turns on when the policy has ``incubation=True``
(:class:`IncubationPolicy`, or ``GoLivePolicy`` once it carries the field).
Its defaults are 63 days and 20 trades, and it adds:

- ``min_days`` becomes ``max(min_days, MinTRL)``: the minimum track record
  length (``stats.min_trl``) of the ``oos`` report's Sharpe, in bars
  (trading days), capped at ``min_trl_cap_days``. A stored ``min_trl_bars``
  metric wins. No Sharpe fails the check;
- ``within_mc_band``: paper max drawdown <= the ``mc_trades`` report's
  ``p95_max_dd`` and, when ``p05_return`` is stored, the paper return >= that
  annual 5th percentile scaled to the period. No Monte Carlo report fails;
- ``quit_rule``: paper drawdown <= ``quit_drawdown_multiple`` x the backtest
  max drawdown (``max_drawdown_oos``), or the Monte Carlo 95th percentile
  when that is tighter;
- the promotion checklist: ``promotion_preset`` (every registered test of
  the preset has a stored report), ``nonzero_costs`` (the lab manifest
  recorded a non-zero cost model), ``hypothesis_recorded`` and
  ``backtest_min_trades`` (the ``oos`` or ``mc_trades`` trade count).

A strategy that shorts (its artifact's ``short_mode`` is ``short``,
roadmap 16.4) gets one more check, ``short_borrow_costs``: its latest
``cost_stress`` report must show the lab charged a realistic borrow fee
(``borrow_fee_rate`` at least ``min_borrow_fee_annual``, default
:data:`MIN_BORROW_FEE_ANNUAL`) and that the edge survived three times that
fee (``sharpe_borrow_stress > 0``). A long-only strategy's report is
unchanged.

Every check reports a value and a limit. :attr:`GoLiveReport.checklist`
carries the promotion context (trial count, DSR, PBO, benchmark excess,
premortem, hypothesis) for the reviewer; it doesn't change the verdict.

The gate only reports. It never changes a strategy's status; promotion
stays a human action (``stonks registry promote``).
"""

from __future__ import annotations

import importlib
import json
import math
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from stonks.accounts.models import DEFAULT_PORTFOLIO_ID
from stonks.config import GoLivePolicy
from stonks.core.protocols import SurvivalReport
from stonks.production.ledger import ledger_filter
from stonks.production.pnl import PnlRow, daily_pnl, load_pnl
from stonks.production.tca import cost_comparison
from stonks.registry.store import StrategyRegistry
from stonks.stats.sharpe import min_trl
from stonks.store.state import SqliteState

PaperSource = Literal["shadow", "portfolio", "none"]

_DAYS_PER_YEAR = 365.25  # matches BacktestReport's CAGR annualization
#: Cost-model inputs of which at least one must be non-zero
#: (``max_impact_bps`` is only a cap, so it doesn't count).
_COST_INPUTS = frozenset({"fee_flat", "fee_bps", "half_spread_bps", "impact_bps"})
#: Lowest annual equity borrow fee that counts as a realistic validation of
#: a short strategy (general collateral runs about 0.25 % to 0.5 % a year).
MIN_BORROW_FEE_ANNUAL = 0.0025


class IncubationPolicy(GoLivePolicy):
    """``GoLivePolicy`` plus the incubation-grade limits (BL-25). The
    integration step moves these fields onto ``GoLivePolicy`` itself;
    until then pass this model to turn the incubation checks on."""

    incubation: bool = True
    min_days: int = Field(default=63, ge=1)
    min_trades: int = Field(default=20, ge=1)
    # Day requirement = max(min_days, MinTRL of the oos Sharpe) when on.
    use_min_trl: bool = True
    min_trl_cap_days: int = Field(default=252, ge=1)
    # The track record must reach PSR >= 1 - alpha.
    min_trl_alpha: float = Field(default=0.05, gt=0.0, lt=1.0)
    # De-annualises the oos Sharpe to a per-bar Sharpe for MinTRL.
    periods_per_year: float = Field(default=252.0, gt=0.0)
    # Quit when the paper drawdown exceeds this multiple of the backtest's.
    quit_drawdown_multiple: float = Field(default=1.5, ge=1.0)
    # Survival preset whose registered tests all need a stored report.
    promotion_preset: str = "promotion"
    # Trades the backtest must have made (oos or mc_trades ``n_trades``).
    min_backtest_trades: int = Field(default=30, ge=1)
    # Characters of recorded hypothesis (lab run or strategy class).
    min_hypothesis_chars: int = Field(default=20, ge=1)


def incubation_policy(policy: Any) -> IncubationPolicy | None:
    """The incubation limits of ``policy``, or ``None`` when it doesn't opt
    in (``incubation`` missing or false). Fields the policy lacks take the
    :class:`IncubationPolicy` defaults."""
    if not getattr(policy, "incubation", False):
        return None
    if isinstance(policy, IncubationPolicy):
        return policy
    fields = {k: getattr(policy, k) for k in IncubationPolicy.model_fields if hasattr(policy, k)}
    return IncubationPolicy(**fields)


@dataclass(frozen=True)
class PaperPeriod:
    strategy_id: str
    status: str
    source: PaperSource
    # P&L rows re-based to the period's first day (returns and drawdown are
    # measured within the period, not from portfolio inception).
    rows: list[PnlRow]
    trades: int
    reports: list[SurvivalReport]
    #: The artifact's ``meta.json`` (lab provenance: hypothesis, manifest, ...).
    meta: dict[str, Any] = field(default_factory=dict)
    #: The strategy class's ``hypothesis`` attribute (BL-26); "" when none.
    strategy_hypothesis: str = ""
    #: The artifact's ``params.json``; ``{}`` when missing.
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def shorts(self) -> bool:
        """The strategy was registered with ``short_mode = "short"``."""
        return self.params.get("short_mode") == "short"

    def report(self, test_id: str) -> SurvivalReport | None:
        """The latest stored survival report for ``test_id``."""
        return next((r for r in reversed(self.reports) if r.test_id == test_id), None)

    def metric(self, test_id: str, key: str) -> float | None:
        """A finite numeric metric of the latest ``test_id`` report."""
        report = self.report(test_id)
        return None if report is None else _finite(report.metrics.get(key))

    @property
    def hypothesis(self) -> str:
        """The recorded hypothesis: the lab run's, else the class's."""
        text = self.meta.get("hypothesis")
        if isinstance(text, str) and text.strip():
            return text.strip()
        return self.strategy_hypothesis.strip()

    @property
    def years(self) -> float:
        """Calendar span of the period in years (0 with fewer than two rows)."""
        if len(self.rows) < 2:
            return 0.0
        return (self.rows[-1].day - self.rows[0].day).days / _DAYS_PER_YEAR

    @property
    def days(self) -> int:
        return len(self.rows)

    @property
    def period_return(self) -> float | None:
        return self.rows[-1].cumulative_return if self.rows else None

    @property
    def max_drawdown(self) -> float | None:
        """Deepest drawdown as a positive fraction; None without data."""
        if not self.rows:
            return None
        return -min(r.drawdown for r in self.rows)

    @property
    def expected_cagr(self) -> float | None:
        report = self.report("oos")
        if report is None:
            return None
        value = _finite(report.metrics.get("cagr_oos"))
        return value if value is not None and value > -1.0 else None

    @property
    def expected_return(self) -> float | None:
        """Backtest-expected return over the period's calendar span."""
        cagr = self.expected_cagr
        if cagr is None or not self.rows:
            return None
        return _compound(cagr, self.years)

    @property
    def drift(self) -> float | None:
        """Paper return minus expected return; None when either is unknown."""
        live, expected = self.period_return, self.expected_return
        if live is None or expected is None:
            return None
        return live - expected


@dataclass(frozen=True)
class GoLiveCheck:
    name: str
    passed: bool
    value: float | None
    limit: float | None
    detail: str


@dataclass(frozen=True)
class GoLiveReport:
    strategy_id: str
    status: str
    source: PaperSource
    checks: list[GoLiveCheck]
    #: Promotion context for the reviewer (see :func:`promotion_checklist`).
    checklist: dict[str, Any] = field(default_factory=dict)
    #: Live shortfall against the modelled cost of the strategy's real
    #: orders (``production.tca.cost_comparison``); empty without a real
    #: paper period.
    costs: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(c.passed for c in self.checks)

    @property
    def failures(self) -> list[GoLiveCheck]:
        return [c for c in self.checks if not c.passed]


def load_paper_period(
    state: SqliteState,
    registry: StrategyRegistry,
    strategy_id: str,
    since: date | None = None,
) -> PaperPeriod:
    """The strategy's paper period. Raises ``KeyError`` for an unknown id."""
    handle = next((h for h in registry.list_all() if h.id == strategy_id), None)
    if handle is None:
        raise KeyError(strategy_id)
    reports = registry.get_reports(strategy_id)

    source: PaperSource
    if handle.status == "shadow":
        source = "shadow"
        raw = load_pnl(state, since=since, strategy_id=strategy_id)
        trades = _shadow_trades(state, strategy_id, since)
    elif handle.status == "active":
        source = "portfolio"
        raw = load_pnl(state, since=since, portfolio_id=DEFAULT_PORTFOLIO_ID)
        trades = _real_trades(state, strategy_id, since)
    else:
        source, raw, trades = "none", [], 0

    rows = daily_pnl([(r.day, r.total_value) for r in raw])
    return PaperPeriod(
        strategy_id=strategy_id,
        status=handle.status,
        source=source,
        rows=rows,
        trades=trades,
        reports=reports,
        meta=_artifact_meta(handle.artifact_path),
        strategy_hypothesis=_class_hypothesis(handle.class_path),
        params=_artifact_json(handle.artifact_path, "params.json"),
    )


def evaluate_golive(
    state: SqliteState,
    registry: StrategyRegistry,
    strategy_id: str,
    policy: GoLivePolicy,
    since: date | None = None,
) -> GoLiveReport:
    period = load_paper_period(state, registry, strategy_id, since=since)
    return GoLiveReport(
        strategy_id=strategy_id,
        status=period.status,
        source=period.source,
        checks=gate_checks(period, policy),
        checklist=promotion_checklist(period),
        costs=(
            cost_comparison(state, strategy_id, DEFAULT_PORTFOLIO_ID, since=since)
            if period.source == "portfolio"
            else {}
        ),
    )


def gate_checks(period: PaperPeriod, policy: GoLivePolicy) -> list[GoLiveCheck]:
    """Every check of the gate, legacy ones first. The incubation checks
    are appended when ``policy`` opts in (see :func:`incubation_policy`)."""
    inc = incubation_policy(policy)
    checks = [
        _status_check(period),
        _min_days_check(period, policy, inc),
        _max_drawdown_check(period, policy),
        _max_drift_check(period, policy),
        _min_trades_check(period, policy),
        _survival_check(period, policy),
    ]
    if period.shorts:
        checks.append(_short_borrow_check(period, policy))
    if inc is not None:
        checks += [
            _mc_band_check(period),
            _quit_rule_check(period, inc),
            _promotion_preset_check(period, inc),
            _nonzero_costs_check(period),
            _hypothesis_check(period, inc),
            _backtest_trades_check(period, inc),
        ]
    return checks


def promotion_checklist(period: PaperPeriod) -> dict[str, Any]:
    """What a reviewer reads before promoting: the trial count of the lab
    run's class, deflated Sharpe, PBO, benchmark excess CAGR, premortem and
    hypothesis. ``None`` for anything not recorded."""
    n_trials = period.meta.get("n_trials_total")
    premortem = period.meta.get("premortem")
    return {
        "n_trials_class": (
            n_trials if isinstance(n_trials, int) and not isinstance(n_trials, bool) else None
        ),
        "dsr": period.metric("deflated_sharpe", "dsr"),
        "pbo": period.metric("pbo", "pbo"),
        "excess_cagr": period.metric("benchmark_relative", "excess_cagr"),
        "premortem": premortem.strip()
        if isinstance(premortem, str) and premortem.strip()
        else None,
        "hypothesis": period.hypothesis or None,
    }


# ---- legacy checks -----------------------------------------------------------


def _status_check(period: PaperPeriod) -> GoLiveCheck:
    ok = period.source != "none"
    status = _STATUS_WORDS.get(period.status, period.status.capitalize())
    detail = (
        f"{status}, measured on {_SOURCE_WORDS[period.source]}"
        if ok
        else f"{status}, so it has no paper trading record"
    )
    return GoLiveCheck(name="status", passed=ok, value=None, limit=None, detail=detail)


def _min_days_check(
    period: PaperPeriod, policy: GoLivePolicy, inc: IncubationPolicy | None
) -> GoLiveCheck:
    if inc is None or not inc.use_min_trl:
        return GoLiveCheck(
            name="min_days",
            passed=period.days >= policy.min_days,
            value=period.days,
            limit=policy.min_days,
            detail=f"{_paper_days(period.days)}, needs at least {policy.min_days}",
        )
    trl, source = _min_trl_bars(period, inc)
    if trl is None:
        return GoLiveCheck(
            name="min_days",
            passed=False,
            value=period.days,
            limit=inc.min_days,
            detail=(
                f"{_paper_days(period.days)}, cannot work out the minimum track record: {source}"
            ),
        )
    trl_days = (
        inc.min_trl_cap_days if math.isinf(trl) else min(math.ceil(trl), inc.min_trl_cap_days)
    )
    need = max(inc.min_days, trl_days)
    trl_text = "an endless" if math.isinf(trl) else f"a {trl:.0f} day"
    return GoLiveCheck(
        name="min_days",
        passed=period.days >= need,
        value=period.days,
        limit=need,
        detail=(
            f"{_paper_days(period.days)}, needs at least {need}: the longer of "
            f"{inc.min_days} days and {trl_text} minimum track record from {source} "
            f"(at most {inc.min_trl_cap_days} days)"
        ),
    )


def _max_drawdown_check(period: PaperPeriod, policy: GoLivePolicy) -> GoLiveCheck:
    dd = period.max_drawdown
    return GoLiveCheck(
        name="max_drawdown",
        passed=dd is not None and dd <= policy.max_drawdown,
        value=dd,
        limit=policy.max_drawdown,
        detail=(
            _NO_PAPER_RESULTS
            if dd is None
            else f"Deepest fall {abs(dd):.2%}, limit {policy.max_drawdown:.2%}"
        ),
    )


def _max_drift_check(period: PaperPeriod, policy: GoLivePolicy) -> GoLiveCheck:
    drift = period.drift
    if drift is not None:
        detail = (
            f"Paper return {period.period_return:+.2%} vs backtest {period.expected_return:+.2%}"
            f" (gap {drift:+.2%}, limit ±{policy.max_drift:.2%})"
        )
    elif not period.rows:
        detail = _NO_PAPER_RESULTS
    elif period.expected_return is None:
        detail = "The out-of-sample backtest has no expected return to compare with"
    else:
        detail = "Paper return unknown: paper trading started from zero"
    return GoLiveCheck(
        name="max_drift",
        passed=drift is not None and abs(drift) <= policy.max_drift,
        value=drift,
        limit=policy.max_drift,
        detail=detail,
    )


def _min_trades_check(period: PaperPeriod, policy: GoLivePolicy) -> GoLiveCheck:
    return GoLiveCheck(
        name="min_trades",
        passed=period.trades >= policy.min_trades,
        value=period.trades,
        limit=policy.min_trades,
        detail=(
            f"{_count(period.trades, 'paper trade')} filled, needs at least {policy.min_trades}"
        ),
    )


def _survival_check(period: PaperPeriod, policy: GoLivePolicy) -> GoLiveCheck:
    passed_n = sum(1 for r in period.reports if r.passed)
    total_n = len(period.reports)
    failed_ids = [_test_name(r.test_id) for r in period.reports if not r.passed]
    if policy.require_all_survival_passed:
        ok = total_n > 0 and passed_n == total_n
        detail = (
            "No robustness tests on record"
            if total_n == 0
            else f"{passed_n} of {total_n} robustness tests passed"
            + (f", failed: {', '.join(failed_ids)}" if failed_ids else "")
        )
    else:
        ok = True
        detail = f"{passed_n} of {total_n} robustness tests passed (not required)"
    return GoLiveCheck(name="survival", passed=ok, value=passed_n, limit=total_n, detail=detail)


# ---- incubation checks (BL-25) -------------------------------------------------


def _mc_band_check(period: PaperPeriod) -> GoLiveCheck:
    name = "within_mc_band"
    dd = period.max_drawdown
    p95 = _p95_drawdown(period)
    if p95 is None:
        return GoLiveCheck(
            name=name,
            passed=False,
            value=dd,
            limit=None,
            detail="No Monte Carlo test on record, so there is no band to compare with",
        )
    if dd is None:
        return GoLiveCheck(name=name, passed=False, value=None, limit=p95, detail=_NO_PAPER_RESULTS)
    ok = dd <= p95
    detail = f"Paper drawdown {abs(dd):.2%} vs Monte Carlo worst case {p95:.2%}"
    p5 = period.metric("mc_trades", "p05_return")  # mc_trades' metric name
    if p5 is None:
        detail += ", no Monte Carlo return floor on record (drawdown only)"
    else:
        floor = _compound(max(p5, -1.0), period.years)
        live = period.period_return
        if live is None or floor is None:
            ok = False
            detail += ", paper return unknown so it cannot meet the Monte Carlo floor"
        else:
            ok = ok and live >= floor
            detail += f", paper return {live:+.2%} vs Monte Carlo floor {floor:+.2%}"
    return GoLiveCheck(name=name, passed=ok, value=dd, limit=p95, detail=detail)


def _quit_rule_check(period: PaperPeriod, inc: IncubationPolicy) -> GoLiveCheck:
    name = "quit_rule"
    dd = period.max_drawdown
    p95 = _p95_drawdown(period)
    bt = period.metric("oos", "max_drawdown_oos")
    if bt is None:
        return GoLiveCheck(
            name=name,
            passed=False,
            value=dd,
            limit=p95,
            detail="No backtest drawdown on record from the out-of-sample test",
        )
    scaled = inc.quit_drawdown_multiple * abs(bt)
    limit = scaled if p95 is None else min(scaled, p95)
    basis = f"{inc.quit_drawdown_multiple:g}x the backtest's worst fall {abs(bt):.2%}"
    basis += f" = {scaled:.2%}"
    if p95 is not None:
        basis += f", Monte Carlo worst case {p95:.2%}"
    if dd is None:
        return GoLiveCheck(
            name=name,
            passed=False,
            value=None,
            limit=limit,
            detail=f"{_NO_PAPER_RESULTS} ({basis})",
        )
    ok = dd <= limit
    verdict = "Within the quit rule:" if ok else "Quit rule tripped, stop paper trading:"
    return GoLiveCheck(
        name=name,
        passed=ok,
        value=dd,
        limit=limit,
        detail=f"{verdict} paper drawdown {abs(dd):.2%} vs limit {limit:.2%} ({basis})",
    )


def _promotion_preset_check(period: PaperPeriod, inc: IncubationPolicy) -> GoLiveCheck:
    from stonks.lab.survival.registry import resolve_preset

    name = "promotion_preset"
    try:
        required = resolve_preset(inc.promotion_preset)
    except ValueError as exc:
        return GoLiveCheck(name=name, passed=False, value=None, limit=None, detail=str(exc))
    stored = {r.test_id for r in period.reports}
    missing = [t for t in required if t not in stored]
    covered = len(required) - len(missing)
    suite = _suite_name(inc.promotion_preset)
    if not required:
        detail = f"The {suite} has no tests"
    else:
        detail = f"{covered} of {len(required)} tests of the {suite} on record"
        if missing:
            detail += f", missing: {', '.join(_test_name(t) for t in missing)}"
    return GoLiveCheck(
        name=name,
        passed=bool(required) and not missing,
        value=covered,
        limit=len(required),
        detail=detail,
    )


def _nonzero_costs_check(period: PaperPeriod) -> GoLiveCheck:
    name = "nonzero_costs"
    manifest = period.meta.get("manifest")
    costs = manifest.get("costs") if isinstance(manifest, dict) else None
    if not isinstance(costs, dict):
        return GoLiveCheck(
            name=name,
            passed=False,
            value=None,
            limit=1,
            detail="No trading costs on record for the backtest",
        )
    nonzero = sorted(set(_nonzero_cost_inputs(costs)))
    detail = (
        f"The backtest paid {_and_join([_COST_WORDS[k] for k in nonzero])}"
        if nonzero
        else "The backtest ran with zero costs"
    )
    return GoLiveCheck(name=name, passed=bool(nonzero), value=len(nonzero), limit=1, detail=detail)


def _hypothesis_check(period: PaperPeriod, inc: IncubationPolicy) -> GoLiveCheck:
    text = period.hypothesis
    need = inc.min_hypothesis_chars
    detail = (
        "No hypothesis written down"
        if not text
        else f"Hypothesis of {_count(len(text), 'character')}, needs at least {need}"
    )
    return GoLiveCheck(
        name="hypothesis_recorded",
        passed=len(text) >= need,
        value=len(text),
        limit=need,
        detail=detail,
    )


def _backtest_trades_check(period: PaperPeriod, inc: IncubationPolicy) -> GoLiveCheck:
    name = "backtest_min_trades"
    need = inc.min_backtest_trades
    for test_id in ("oos", "mc_trades"):
        n = period.metric(test_id, "n_trades")
        if n is not None:
            return GoLiveCheck(
                name=name,
                passed=n >= need,
                value=int(n),
                limit=need,
                detail=(
                    f"{_count(int(n), 'backtest trade')} in the {_TEST_WORDS[test_id]},"
                    f" needs at least {need}"
                ),
            )
    return GoLiveCheck(
        name=name,
        passed=False,
        value=None,
        limit=need,
        detail="No backtest trade count on record",
    )


def _short_borrow_check(period: PaperPeriod, policy: GoLivePolicy) -> GoLiveCheck:
    """A short strategy was validated with realistic borrow costs."""
    name = "short_borrow_costs"
    need = float(getattr(policy, "min_borrow_fee_annual", MIN_BORROW_FEE_ANNUAL))
    if period.report("cost_stress") is None:
        return GoLiveCheck(
            name=name,
            passed=False,
            value=None,
            limit=need,
            detail="No cost stress test on record",
        )
    fee = period.metric("cost_stress", "borrow_fee_rate")
    stressed = period.metric("cost_stress", "sharpe_borrow_stress")
    if fee is None or stressed is None:
        return GoLiveCheck(
            name=name,
            passed=False,
            value=None,
            limit=need,
            detail="Tested without borrow costs: the test data had no short trades",
        )
    failures = []
    if fee < need:
        failures.append(f"borrow fee {fee:.2%} a year is below {need:.2%}")
    if not stressed > 0:
        failures.append(f"Sharpe {stressed:.2f} at triple the borrow fee is not above zero")
    detail = (
        ", ".join(failures).capitalize()
        if failures
        else f"Borrow fee {fee:.2%} a year, Sharpe {stressed:.2f} at triple the fee"
    )
    return GoLiveCheck(name=name, passed=not failures, value=fee, limit=need, detail=detail)


# ---- helpers ------------------------------------------------------------------

# Check details are shown to traders as is, so they use the console's words
# (docs/ui.md, "Words across surfaces"): no status keys, test ids or metric
# names.
_STATUS_WORDS = {"shadow": "Paper trading", "active": "Live", "retired": "Stopped"}
_SOURCE_WORDS: dict[PaperSource, str] = {
    "shadow": "its paper trading results",
    "portfolio": "the main portfolio's results",
    "none": "nothing",
}
_TEST_WORDS = {"oos": "out-of-sample test", "mc_trades": "Monte Carlo test"}
_COST_WORDS = {
    "fee_flat": "a flat fee",
    "fee_bps": "a fee",
    "half_spread_bps": "the spread",
    "impact_bps": "market impact",
}
_NO_PAPER_RESULTS = "No paper trading results yet"


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _and_join(words: list[str]) -> str:
    """``a, b and c``."""
    return words[0] if len(words) == 1 else f"{', '.join(words[:-1])} and {words[-1]}"


def _paper_days(n: int) -> str:
    return f"{_count(n, 'day')} of paper trading"


#: The console's names of the robustness tests
#: (``web/src/app/shared/lab-results/survival-tests.ts``).
_TEST_NAMES = {
    "oos": "Out of sample",
    "period_stability": "Period stability",
    "perturbation": "Perturbation",
    "walk_forward": "Walk-forward",
    "deflated_sharpe": "Deflated Sharpe",
    "pbo": "Overfitting (PBO)",
    "data_snooping": "Data snooping (SPA)",
    "mc_trades": "Monte Carlo trades",
    "cost_stress": "Cost stress",
    "plateau": "Parameter plateau",
    "cross_instrument": "Cross-instrument",
    "benchmark_relative": "Beats the benchmark",
    "mcpt": "Monte Carlo permutation",
    "permutation": "Monte Carlo permutation",
    "drift": "Drift",
    "runs_test": "Runs test",
    "walk_forward_mcpt": "Walk-forward permutation",
    "cpcv": "Combinatorial purged CV",
    "crisis": "Crisis periods",
    "event_study": "Event study",
    "vs_random": "Beats random entries",
    "signal_ic": "Signal IC",
    "pool_correlation": "Pool correlation",
    "stress": "Stress",
}


def _test_name(test_id: str) -> str:
    """A robustness test's console name; an unknown id in plain words."""
    return _TEST_NAMES.get(test_id) or test_id.replace("_", " ").capitalize()


def _suite_name(preset: str) -> str:
    words = preset.replace("_", " ")
    return "full test suite" if preset == "promotion" else f"{words!r} test suite"


def _finite(value: Any) -> float | None:
    """``value`` as a float when it is a finite real number (not a bool)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def _compound(annual: float, years: float) -> float | None:
    try:
        out = (1.0 + annual) ** years - 1.0
    except (OverflowError, ZeroDivisionError):
        return None
    return out if isinstance(out, float) and math.isfinite(out) else None


def _p95_drawdown(period: PaperPeriod) -> float | None:
    """The Monte Carlo 95th-percentile drawdown as a positive fraction."""
    value = period.metric("mc_trades", "p95_max_dd")
    return None if value is None else abs(value)


def _min_trl_bars(period: PaperPeriod, inc: IncubationPolicy) -> tuple[float | None, str]:
    """MinTRL in bars (may be infinite) and where it came from; ``None``
    with the reason when it can't be had."""
    report = period.report("oos")
    if report is None:
        return None, "no out-of-sample test on record"
    stored = report.metrics.get("min_trl_bars")
    if (
        not isinstance(stored, bool)
        and isinstance(stored, int | float)
        and not math.isnan(stored)
        and stored > 0
    ):
        return float(stored), "the out-of-sample test"
    sharpe = _finite(report.metrics.get("sharpe_oos"))
    if sharpe is None:
        return None, "the out-of-sample test has no Sharpe"
    skew = _finite(report.metrics.get("skew"))
    kurt = _finite(report.metrics.get("kurtosis"))  # non-excess (normal = 3)
    try:
        trl = min_trl(
            sharpe / math.sqrt(inc.periods_per_year),
            0.0,
            0.0 if skew is None else skew,
            3.0 if kurt is None else kurt,
            alpha=inc.min_trl_alpha,
        )
    except ValueError as exc:
        return None, f"out-of-sample Sharpe {sharpe:.2f} gives none ({exc})"
    if math.isnan(trl) or trl <= 0:
        return None, f"out-of-sample Sharpe {sharpe:.2f} gives none"
    return trl, f"the out-of-sample Sharpe {sharpe:.2f}"


def _nonzero_cost_inputs(costs: Any) -> list[str]:
    """Names of the cost inputs anywhere in ``costs`` that are above zero."""
    found: list[str] = []
    if isinstance(costs, dict):
        for key, value in costs.items():
            if key in _COST_INPUTS:
                number = _finite(value)
                if number is not None and number > 0:
                    found.append(key)
            else:
                found.extend(_nonzero_cost_inputs(value))
    return found


def _artifact_meta(artifact_path: Path) -> dict[str, Any]:
    """The artifact's ``meta.json``; ``{}`` when missing or unreadable."""
    return _artifact_json(artifact_path, "meta.json")


def _artifact_json(artifact_path: Path, name: str) -> dict[str, Any]:
    """A JSON object file of the artifact; ``{}`` when missing or unreadable."""
    try:
        loaded = json.loads((Path(artifact_path) / name).read_text())
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _class_hypothesis(class_path: str) -> str:
    """The strategy class's ``hypothesis``; "" when it can't be imported."""
    module_name, _, cls_name = class_path.partition(":")
    try:
        cls = getattr(importlib.import_module(module_name), cls_name)
    except (ImportError, AttributeError, ValueError):
        return ""
    text = getattr(cls, "hypothesis", "")
    return text if isinstance(text, str) else ""


def _shadow_trades(state: SqliteState, strategy_id: str, since: date | None) -> int:
    rows = state.sql(
        "SELECT COUNT(*) AS n FROM shadow_decisions "
        "WHERE strategy_id = ? AND status = 'filled' AND as_of >= ?",
        [strategy_id, since.isoformat() if since else ""],
    )
    return int(rows[0]["n"])


def _real_trades(state: SqliteState, strategy_id: str, since: date | None) -> int:
    """Fills of the strategy's orders in the default portfolio (the real
    book go-live evidence reads; other people's portfolios never count)."""
    where, params = ledger_filter(state, "fills", DEFAULT_PORTFOLIO_ID, alias="f")
    rows = state.sql(
        "SELECT COUNT(*) AS n FROM fills f JOIN orders o ON o.client_id = f.order_client_id "
        f"WHERE o.strategy_id = ? AND substr(f.filled_at, 1, 10) >= ? AND {where}",
        [strategy_id, since.isoformat() if since else "", *params],
    )
    return int(rows[0]["n"])
