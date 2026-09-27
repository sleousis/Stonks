"""QuantValue — Gray & Carlisle's *Quantitative Value* (BL-41).

``mode="quant_value"`` (default), on each rebalance:

1. **Universe.** The ``universe`` param or the lake's equities, minus the
   ``excluded_sectors`` (financials, utilities and real estate by default:
   their statements don't fit EBIT/TEV). A name with an unknown sector stays.
2. **Eligibility.** A positive EBIT/TEV, a financial-strength score and at
   least ``min_years`` consecutive annual reports (franchise power degrades
   to the years available, up to ``max_years``).
3. **Forensic screens.** Drop the worst ``forensic_drop_pct`` (rounded
   down) of the eligible names on each of scaled total accruals (STA),
   scaled net operating assets (SNOA) and probability of manipulation
   (PMAN, from the Beneish M-score); drop names with an Altman Z below
   ``altman_z_min`` (financial distress). A screen that can't be computed
   for a name doesn't drop it.
4. **Value.** Keep the cheapest ``value_pct`` (rounded up) by EBIT/TEV.
5. **Quality.** Rank those by ``0.5 pct(franchise power) + 0.5 pct(FS)``
   (percentiles within the cheap slice) and hold the top ``n``, equally
   weighted.

``mode="piotroski"``: the top ``bm_pct`` of the universe by book-to-market
(positive book only), holding those with a Piotroski F-score of at least
``f_min`` (best F first, at most ``n``). ``mode="magic_formula"``
(Greenblatt): the ``n`` lowest rank sums of EBIT/TEV and return on capital.

All modes rebalance on the last session of the ``rebalance_months`` (annual
by default, or semi-annual) and hold in between. Selected names get
``estimate_return = 1.0``.

Point in time: statements come through :class:`QualityValue`'s readers, so a
row is visible only from its filing date (``period_end +
missing_filing_lag_days`` when the vendor gave none). EBIT is trailing
twelve months (or the latest annual report); balance items are the latest
visible report. Annual history must be consecutive fiscal years ending in
a report no older than ``max_statement_age_days``. A name whose last close is
more than ten days old (delisted, halted) has no market cap and never ranks.
Formulas live in :mod:`stonks.features.fundamentals`.
"""

from __future__ import annotations

import math
import weakref
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd

from stonks.core.params import ParameterSpec
from stonks.core.types import Features, Order, Portfolio
from stonks.features import fundamentals as fx
from stonks.features.momentum import is_quarter_rebalance_day
from stonks.portfolio.base import ConstructionInput, get_constructor
from stonks.strategies._common import as_datetime
from stonks.strategies.base import BaseStrategy
from stonks.strategies.examples._cross_section import (
    CrossSectionMemo,
    lake_universe,
    orders_from_constructor,
    parse_universe,
    session_cutoff,
)
from stonks.strategies.examples.quality_value import QualityValue, _is_intraday

MODES = ("quant_value", "piotroski", "magic_formula")
_EPS = 1e-9
_DEFAULT_EXCLUDED = "Financial Services,Financials,Utilities,Real Estate"
_REBALANCE_MONTHS = [str(m) for m in range(1, 13)] + [f"{m},{m + 6}" for m in range(1, 7)]
# Consecutive fiscal years are 300-430 days apart (52/53-week years and
# a changed fiscal year end fit; a skipped year doesn't).
_YEAR_GAP_DAYS = (300, 430)

_TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "income_statement": (
        "revenue",
        "cost_of_revenue",
        "gross_profit",
        "selling_general_administrative",
        "ebit",
        "operating_income",
        "net_income",
        "net_income_continuing",
        "depreciation_amortization",
        "reconciled_depreciation",
    ),
    "balance_sheet": (
        "total_assets",
        "current_assets",
        "current_liabilities",
        "cash",
        "cash_and_equivalents",
        "cash_and_short_term_investments",
        "net_receivables",
        "property_plant_equipment_net",
        "short_term_debt",
        "long_term_debt",
        "long_term_debt_total",
        "short_long_term_debt_total",
        "total_liabilities",
        "total_stockholder_equity",
        "retained_earnings",
        "preferred_stock_total_equity",
        "noncontrolling_interest",
        "common_stock_shares_outstanding",
    ),
    "cash_flow_statement": (
        "operating_cash_flow",
        "free_cash_flow",
        "capital_expenditures",
        "depreciation",
    ),
}
_FP_COLUMNS = ("roa", "roc", "cfoa", "margin_growth", "margin_stability")


# ---- selection (pure) -----------------------------------------------------------------


def _val(m: Mapping[str, Any], key: str) -> float | None:
    v = m.get(key)
    if v is None:
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def _worst(metrics: Mapping[str, Mapping[str, Any]], names: Sequence[str], key: str, pct: float):
    """The ``floor(pct * k)`` names with the highest ``key`` among the
    ``k`` names that have one."""
    scored = [(v, t) for t in names if (v := _val(metrics[t], key)) is not None]
    k = math.floor(pct * len(scored) + _EPS)
    return {t for _, t in sorted(scored, key=lambda p: (-p[0], p[1]))[:k]}


def select_quant_value(
    metrics: Mapping[str, Mapping[str, Any]],
    *,
    n: int,
    value_pct: float,
    forensic_drop_pct: float,
    altman_z_min: float = 1.81,
    min_years: int = 3,
) -> list[str]:
    """Gray & Carlisle's pipeline over ``{ticker: metrics}``: screen, then
    value, then quality. Eligible names have a positive ``ebit_tev``, an
    ``fs`` score and ``years_used >= min_years``. Returns the held names,
    best first."""
    eligible = sorted(
        t
        for t, m in metrics.items()
        if (_val(m, "ebit_tev") or 0.0) > 0
        and _val(m, "fs") is not None
        and (_val(m, "years_used") or 0.0) >= min_years
    )
    dropped: set[str] = set()
    for key in ("sta", "snoa", "pman"):
        dropped |= _worst(metrics, eligible, key, forensic_drop_pct)
    for t in eligible:
        z = _val(metrics[t], "altman_z")
        if z is not None and z < altman_z_min:
            dropped.add(t)
    survivors = [t for t in eligible if t not in dropped]
    if not survivors:
        return []

    k = max(1, math.ceil(value_pct * len(survivors) - _EPS))
    cheap = sorted(survivors, key=lambda t: (-_val(metrics[t], "ebit_tev"), t))[:k]  # type: ignore[operator]

    frame = pd.DataFrame(
        {c: [_val(metrics[t], f"fp_{c}") for t in cheap] for c in _FP_COLUMNS},
        index=cheap,
        dtype=float,
    )
    fp = fx.pct_rank(fx.franchise_power(frame))
    fs = fx.pct_rank(pd.Series({t: _val(metrics[t], "fs") for t in cheap}, dtype=float))
    quality = pd.concat([fp, fs], axis=1).mean(axis=1, skipna=True)
    ranked = sorted(cheap, key=lambda t: (-quality[t], -_val(metrics[t], "ebit_tev"), t))  # type: ignore[operator]
    return ranked[: max(1, n)]


def select_piotroski(
    metrics: Mapping[str, Mapping[str, Any]], *, bm_pct: float, f_min: int, n: int
) -> list[str]:
    """Piotroski (2000): the top ``bm_pct`` (rounded up) by book-to-market
    among names with positive book equity and an F-score, keeping F >=
    ``f_min``. Best F first (then higher book-to-market), at most ``n``."""
    eligible = [
        t
        for t, m in metrics.items()
        if (_val(m, "bm") or 0.0) > 0 and _val(m, "f_score") is not None
    ]
    if not eligible:
        return []
    k = max(1, math.ceil(bm_pct * len(eligible) - _EPS))
    value = sorted(eligible, key=lambda t: (-_val(metrics[t], "bm"), t))[:k]  # type: ignore[operator]
    strong = [t for t in value if _val(metrics[t], "f_score") >= f_min]  # type: ignore[operator]
    return sorted(
        strong,
        key=lambda t: (-_val(metrics[t], "f_score"), -_val(metrics[t], "bm"), t),  # type: ignore[operator]
    )[: max(1, n)]


def select_magic_formula(metrics: Mapping[str, Mapping[str, Any]], *, n: int) -> list[str]:
    """Greenblatt: rank by EBIT/TEV and by return on capital (1 = best),
    hold the ``n`` lowest rank sums. Needs positive EBIT/TEV and a ROC."""
    eligible = [
        t
        for t, m in metrics.items()
        if (_val(m, "ebit_tev") or 0.0) > 0 and _val(m, "roc") is not None
    ]
    if not eligible:
        return []
    ey = pd.Series({t: _val(metrics[t], "ebit_tev") for t in eligible}, dtype=float)
    rc = pd.Series({t: _val(metrics[t], "roc") for t in eligible}, dtype=float)
    total = ey.rank(ascending=False, method="min") + rc.rank(ascending=False, method="min")
    return sorted(eligible, key=lambda t: (total[t], -ey[t], t))[: max(1, n)]


# ---- strategy ------------------------------------------------------------------------------


class QuantValue(BaseStrategy):
    id = "quant_value"
    hypothesis = (
        "Systematic value beats behavioural mispricing: investors overextrapolate "
        "poor recent results, so cheap stocks by EBIT/TEV are priced too low and "
        "rerate. The losers are sellers anchored on the bad news. The forensic "
        "screens (accruals, NOA, Beneish manipulation, Altman distress) remove the "
        "cheap names that are cheap for a reason, avoiding permanent loss of "
        "capital. Fails in long growth-led markets (value droughts such as "
        "2017-2020) and when accounting data are late, restated or fraudulent in "
        "ways the screens miss."
    )
    alpha_family = "value"
    premise = "mean_reversion"
    label_horizon_bars = 252
    required_history_bars = 1

    def __init__(self, params: Any) -> None:
        super().__init__(params)
        if self.params["mode"] not in MODES:
            raise ValueError(f"mode must be one of {MODES}, got {self.params['mode']!r}")
        if int(self.params["min_years"]) > int(self.params["max_years"]):
            raise ValueError("min_years must not exceed max_years")
        # Point-in-time statement, price and share-count readers, shared
        # with QualityValue so both apply the same filing-date rules.
        self._pit = QualityValue(
            {
                "missing_filing_lag_days": int(self.params["missing_filing_lag_days"]),
                "max_statement_age_days": int(self.params["max_statement_age_days"]),
            }
        )
        self._memo = CrossSectionMemo()
        self._annual: weakref.WeakKeyDictionary[Any, dict[tuple, Any]]
        self._annual = weakref.WeakKeyDictionary()

    @classmethod
    def parameter_spec(cls):
        return [
            ParameterSpec(
                name="mode",
                kind="categorical",
                default="quant_value",
                bounds=list(MODES),
                tunable=False,
                description="quant_value (Gray & Carlisle), piotroski, or magic_formula.",
            ),
            ParameterSpec(
                name="n",
                kind="int",
                default=30,
                bounds=(1, 100),
                description="Most names held, equally weighted.",
            ),
            ParameterSpec(
                name="value_pct",
                kind="float",
                default=0.10,
                bounds=(0.02, 1.0),
                description="Cheapest fraction by EBIT/TEV kept for the quality rank.",
            ),
            ParameterSpec(
                name="forensic_drop_pct",
                kind="float",
                default=0.05,
                bounds=(0.0, 0.2),
                description="Worst fraction dropped on each of STA, SNOA and PMAN.",
            ),
            ParameterSpec(
                name="altman_z_min",
                kind="float",
                default=1.81,
                bounds=(0.0, 3.0),
                tunable=False,
                description="Altman Z below this is financial distress (dropped).",
            ),
            ParameterSpec(
                name="min_years",
                kind="int",
                default=3,
                bounds=(2, 8),
                tunable=False,
                description="Fewest consecutive annual reports for franchise power.",
            ),
            ParameterSpec(
                name="max_years",
                kind="int",
                default=8,
                bounds=(2, 10),
                tunable=False,
                description="Most annual reports used for franchise power.",
            ),
            ParameterSpec(
                name="bm_pct",
                kind="float",
                default=0.2,
                bounds=(0.05, 1.0),
                description="piotroski: top book-to-market fraction considered.",
            ),
            ParameterSpec(
                name="f_min",
                kind="int",
                default=7,
                bounds=(5, 9),
                description="piotroski: lowest F-score held.",
            ),
            ParameterSpec(
                name="rebalance_months",
                kind="categorical",
                default="6",
                bounds=_REBALANCE_MONTHS,
                tunable=False,
                description="Months whose last session rebalances (one = annual, two = semi-annual).",
            ),
            ParameterSpec(
                name="excluded_sectors",
                kind="categorical",
                default=_DEFAULT_EXCLUDED,
                bounds=None,
                tunable=False,
                description="Comma-separated sectors never held (case-insensitive).",
            ),
            ParameterSpec(
                name="universe",
                kind="categorical",
                default="",
                bounds=None,
                tunable=False,
                description="Comma-separated tickers ranked together; empty = the lake's.",
            ),
            ParameterSpec(
                name="missing_filing_lag_days",
                kind="int",
                default=90,
                bounds=(0, 365),
                tunable=False,
                description="Days after period end a statement with no filing date "
                "is treated as public.",
            ),
            ParameterSpec(
                name="max_statement_age_days",
                kind="int",
                default=550,
                bounds=(90, 1500),
                tunable=False,
                description="Ignore statements whose period ended longer ago than this.",
            ),
        ]

    # ---- Strategy Protocol ----------------------------------------------------------

    def extract_features(self, ticker: str, as_of: Any, lake: Any) -> Features:
        if lake is None:
            return Features(values={})
        metrics = self._metrics(ticker, as_of, lake)
        return Features(values={k: float(v) for k, v in metrics.items() if v is not None})

    def score_universe(self, tickers: Sequence[str], as_of: Any, lake: Any) -> dict[str, float]:
        """``{ticker: 1.0}`` for the names held from ``tickers`` as of ``as_of``."""
        names = self._drop_excluded_sectors(list(dict.fromkeys(tickers)), lake)
        metrics = {t: self._metrics(t, as_of, lake) for t in names}
        mode, n = self.params["mode"], int(self.params["n"])
        if mode == "piotroski":
            kept = select_piotroski(
                metrics, bm_pct=float(self.params["bm_pct"]), f_min=int(self.params["f_min"]), n=n
            )
        elif mode == "magic_formula":
            kept = select_magic_formula(metrics, n=n)
        else:
            kept = select_quant_value(
                metrics,
                n=n,
                value_pct=float(self.params["value_pct"]),
                forensic_drop_pct=float(self.params["forensic_drop_pct"]),
                altman_z_min=float(self.params["altman_z_min"]),
                min_years=int(self.params["min_years"]),
            )
        return dict.fromkeys(kept, 1.0)

    def estimate_return(self, ticker: str, as_of: Any, lake: Any) -> float | None:
        if lake is None:
            return None
        day, _ = session_cutoff(as_of)
        selected = self._memo.get(
            lake, day, lambda: self.score_universe(self._universe(lake), as_of, lake)
        )
        return selected.get(ticker)

    def decide(
        self,
        my_picks: Sequence[tuple[float, str]],
        portfolio: Portfolio,
        prices: Mapping[str, float],
        as_of: Any,
    ) -> list[Order]:
        day, _ = session_cutoff(as_of)
        months = [int(m) for m in str(self.params["rebalance_months"]).split(",")]
        if not is_quarter_rebalance_day(day, months):
            return []
        signals = {t: 1.0 for r, t in my_picks if r > 0}
        inp = ConstructionInput(
            signals={self.id: signals}, portfolio=portfolio, prices=prices, as_of=day
        )
        constructor = get_constructor("equal_weight_top_n", n=max(1, len(signals)))
        return orders_from_constructor(constructor, inp, strategy_id=self.id)

    # ---- universe ---------------------------------------------------------------------

    def _universe(self, lake: Any) -> list[str]:
        explicit = parse_universe(str(self.params["universe"]))
        if explicit:
            return explicit
        return self._memo.universe(lake, lambda: lake_universe(lake, self.applicable_asset_classes))

    def _drop_excluded_sectors(self, tickers: list[str], lake: Any) -> list[str]:
        excluded = {
            s.strip().lower() for s in str(self.params["excluded_sectors"]).split(",") if s.strip()
        }
        if not excluded or not tickers or lake is None:
            return tickers
        reader: Any = getattr(lake, "instrument_sectors", None)
        if callable(reader):  # a typed read, so a point-in-time lake allows it (BL-49)
            rows: Any = reader(tickers)
        else:
            rows = lake.sql(
                "SELECT id, sector, gic_sector FROM instruments WHERE id = ANY(?)", [tickers]
            )
        banned = {
            str(r.id)
            for r in rows.itertuples()
            if any(
                isinstance(s, str) and s.strip().lower() in excluded
                for s in (r.sector, r.gic_sector)
            )
        }
        return [t for t in tickers if t not in banned]

    # ---- per-ticker metrics -----------------------------------------------------------

    def _metrics(self, ticker: str, as_of: Any, lake: Any) -> dict[str, float | None]:
        day = as_datetime(as_of).date()
        # Day-stamped rows (share counts) are known once their day ends.
        # A filing is used from the day after it, which ``available_date``
        # already holds, daily or intraday (BE-22).
        known = day - timedelta(days=1) if _is_intraday(as_of) else day
        oldest = day - timedelta(days=int(self.params["max_statement_age_days"]))

        years = self._annual_years(lake, ticker, day, oldest)
        balance = self._latest_balance(lake, ticker, day, oldest)
        ebit_snap = self._pit._flows(
            lake, "income_statement", ticker, day, ("ebit", "operating_income")
        )
        ebit = None
        if ebit_snap is not None and ebit_snap.period_end >= oldest:
            ebit = fx.ebit(ebit_snap.values)
        if balance is None and not years:
            return {}

        balance = balance or {}
        price = self._pit._price(lake, ticker, as_of, day)
        shares = self._pit._shares(lake, ticker, known, balance)
        mcap = price * shares if price is not None and shares is not None else None
        tev = fx.tev(mcap, balance)
        equity = fx.line(balance, "total_stockholder_equity")

        out: dict[str, float | None] = {
            "market_cap": mcap,
            "tev": tev,
            "ebit_tev": fx.ebit_tev(ebit, tev),
            "roc": fx.roc(ebit, balance),
            "bm": None if mcap is None or equity is None else equity / mcap,
            "snoa": fx.snoa(balance),
            "years_used": float(len(years)),
        }
        if len(years) >= 2:
            cur, prev = years[-1], years[-2]
            m = fx.beneish_m(cur, prev)
            out.update(
                sta=fx.sta(cur, prev),
                beneish_m=m,
                pman=fx.pman(m),
                altman_z=fx.altman_z(cur, mcap),
                fs=_as_float(fx.fs_score(cur, prev)),
                f_score=_as_float(fx.piotroski_f(cur, prev)),
            )
        fp = fx.franchise_inputs(years, min_years=int(self.params["min_years"]))
        if fp is not None:
            out.update({f"fp_{c}": fp[c] for c in _FP_COLUMNS})
        return out

    def _annual_years(self, lake: Any, ticker: str, day: date, oldest: date) -> list[dict]:
        """Consecutive annual reports visible on ``day`` (oldest first, at
        most ``max_years``), income and balance required, cash flow merged
        when present. Empty when the latest is older than ``oldest``."""
        hists = {t: self._pit._history(lake, t, ticker) for t in _TABLE_COLUMNS}
        masks = {t: h.visible(day) & (h.frequency == "A") for t, h in hists.items()}
        key = ("annual", ticker, *(m.tobytes() for m in masks.values()))
        per_lake = self._per_lake(lake)
        if key not in per_lake:
            per_lake[key] = self._consecutive_years(hists, masks)
        years = per_lake[key]
        return years if years and years[-1]["_period_end"] >= oldest else []

    def _consecutive_years(self, hists: Mapping[str, Any], masks: Mapping[str, Any]) -> list[dict]:
        """The latest run of consecutive fiscal years among the masked rows,
        each merged across the three statements and tagged ``_period_end``."""
        rows: dict[str, dict[date, dict]] = {}
        for table, hist in hists.items():
            cols = {c: hist.column(c) for c in _TABLE_COLUMNS[table]}
            rows[table] = {
                hist.period_end[i]: {c: v[i] for c, v in cols.items()}
                for i in np.flatnonzero(masks[table])
            }
        ends = sorted(set(rows["income_statement"]) & set(rows["balance_sheet"]))
        run: list[date] = []
        if ends:
            run = [ends[-1]]
            for pe in reversed(ends[:-1]):
                gap = (run[0] - pe).days
                if not _YEAR_GAP_DAYS[0] <= gap <= _YEAR_GAP_DAYS[1]:
                    break
                run.insert(0, pe)
        run = run[-int(self.params["max_years"]) :]
        return [
            {
                **rows["cash_flow_statement"].get(pe, {}),
                **rows["balance_sheet"][pe],
                **rows["income_statement"][pe],
                "_period_end": pe,
            }
            for pe in run
        ]

    def _latest_balance(self, lake: Any, ticker: str, day: date, oldest: date) -> dict | None:
        """The newest visible balance sheet (quarterly or annual)."""
        hist = self._pit._history(lake, "balance_sheet", ticker)
        idx = np.flatnonzero(hist.visible(day))
        if not len(idx):
            return None
        i = int(idx[-1])  # rows are ordered by period end
        if hist.period_end[i] < oldest:
            return None
        key = ("balance", ticker, i)
        per_lake = self._per_lake(lake)
        if key not in per_lake:
            per_lake[key] = {c: hist.column(c)[i] for c in _TABLE_COLUMNS["balance_sheet"]}
        return per_lake[key]

    def _per_lake(self, lake: Any) -> dict:
        try:
            return self._annual.setdefault(lake, {})
        except TypeError:  # a lake that can't be weakly referenced: no memo
            return {}


def _as_float(v: int | None) -> float | None:
    return None if v is None else float(v)
