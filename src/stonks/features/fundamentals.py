"""Fundamental scores for value and forensic screens (BL-41).

Pure functions over statement rows. A *row* is a mapping from lake column
names (``income_statement``, ``balance_sheet``, ``cash_flow_statement``) to
numbers or ``None``; an annual row usually merges the three statements of
one fiscal year. ``cur`` is year ``t`` and ``prev`` is year ``t-1``. None of
these functions know about dates: callers pass only rows that were public
at the decision date.

Conventions:

- A missing line is ``None``. Ratios return ``None`` when a denominator is
  missing, zero or (where the ratio needs it) negative, never ``inf``.
- Debt, preferred stock, minority interest and cash lines that are absent
  count as zero (vendors omit lines that don't apply, e.g. a debt-free firm
  has no ``long_term_debt``).
- Depreciation and capital expenditure are taken as magnitudes (``abs``):
  vendors sign cash-flow outflows differently.

Sources: Gray & Carlisle, *Quantitative Value* (2012) for TEV, STA, SNOA,
PMAN, franchise power and the financial-strength score; Piotroski (2000),
"Value Investing: The Use of Historical Financial Statement Information";
Beneish (1999), "The Detection of Earnings Manipulation"; Altman (1968),
"Financial Ratios, Discriminant Analysis and the Prediction of Corporate
Bankruptcy"; Greenblatt, *The Little Book That Beats the Market* (ROC);
Sloan (1996) and Hirshleifer, Hou, Teoh & Zhang (2004) for accruals and net
operating assets.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
import pandas as pd

Row = Mapping[str, Any]

#: Cap on margin stability (mean / std): a perfectly flat margin history
#: would otherwise be infinite.
MAX_MARGIN_STABILITY = 1000.0


# ---- line items --------------------------------------------------------------------


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def line(row: Row, *names: str) -> float | None:
    """The first of ``names`` that ``row`` has a finite value for."""
    for name in names:
        v = _num(row.get(name))
        if v is not None:
            return v
    return None


def _zero(row: Row, *names: str) -> float:
    v = line(row, *names)
    return 0.0 if v is None else v


def _div(num: float | None, den: float | None) -> float | None:
    """``num / den`` for a non-zero denominator, else ``None``."""
    if num is None or den is None or den == 0:
        return None
    return num / den


def _pos_div(num: float | None, den: float | None) -> float | None:
    """``num / den`` for a strictly positive denominator, else ``None``."""
    if den is None or den <= 0:
        return None
    return _div(num, den)


def ebit(row: Row) -> float | None:
    """Earnings before interest and taxes: ``ebit``, else ``operating_income``."""
    return line(row, "ebit", "operating_income")


def depreciation(row: Row) -> float | None:
    """Depreciation and amortisation (a magnitude), from the income
    statement, else the cash-flow statement."""
    v = line(row, "depreciation_amortization", "reconciled_depreciation", "depreciation")
    return None if v is None else abs(v)


def free_cash_flow(row: Row) -> float | None:
    """``free_cash_flow``, else operating cash flow minus |capex|."""
    fcf = line(row, "free_cash_flow")
    if fcf is not None:
        return fcf
    cfo, capex = line(row, "operating_cash_flow"), line(row, "capital_expenditures")
    return None if cfo is None or capex is None else cfo - abs(capex)


def gross_margin(row: Row) -> float | None:
    """Gross profit / revenue (gross profit falls back to revenue - |COGS|)."""
    revenue = line(row, "revenue")
    gp = line(row, "gross_profit")
    if gp is None and revenue is not None:
        cogs = line(row, "cost_of_revenue")
        gp = None if cogs is None else revenue - abs(cogs)
    return _pos_div(gp, revenue)


def total_debt(row: Row) -> float:
    """``short_long_term_debt_total``, else short-term plus long-term debt
    (absent lines count as zero)."""
    total = line(row, "short_long_term_debt_total")
    if total is not None:
        return total
    return _zero(row, "short_term_debt") + _zero(row, "long_term_debt", "long_term_debt_total")


def _long_term_debt(row: Row) -> float:
    return _zero(row, "long_term_debt", "long_term_debt_total")


def _cash_and_investments(row: Row) -> float:
    return _zero(row, "cash_and_short_term_investments", "cash_and_equivalents", "cash")


# ---- value -------------------------------------------------------------------------


def tev(market_cap: float | None, bs: Row) -> float | None:
    """Total enterprise value (Gray & Carlisle):

    ``TEV = market cap + total debt + preferred stock + minority interest
    - cash and short-term investments``.

    ``None`` without a positive market cap, or when TEV is not positive (a
    net-cash shell has no meaningful earnings yield).
    """
    if market_cap is None or market_cap <= 0:
        return None
    value = (
        market_cap
        + total_debt(bs)
        + _zero(bs, "preferred_stock_total_equity")
        + _zero(bs, "noncontrolling_interest")
        - _cash_and_investments(bs)
    )
    return value if value > 0 else None


def ebit_tev(ebit_value: float | None, tev_value: float | None) -> float | None:
    """Earnings yield on enterprise value, ``EBIT / TEV``. Negative EBIT
    gives a negative yield (it ranks as the most expensive)."""
    return _pos_div(ebit_value, tev_value)


def roc(ebit_value: float | None, bs: Row) -> float | None:
    """Greenblatt's return on capital,
    ``EBIT / (net PP&E + current assets - current liabilities)``.
    ``None`` when capital employed is missing or not positive."""
    ppe = line(bs, "property_plant_equipment_net")
    ca, cl = line(bs, "current_assets"), line(bs, "current_liabilities")
    if ppe is None or ca is None or cl is None:
        return None
    return _pos_div(ebit_value, ppe + ca - cl)


# ---- forensic screens --------------------------------------------------------------


def sta(cur: Row, prev: Row) -> float | None:
    """Scaled total accruals (Sloan 1996; Gray & Carlisle):

    ``STA = (dCA - dCash - (dCL - dSTD - dTP) - Dep) / TA``

    with ``d`` the change from ``prev`` to ``cur`` and ``TA`` this year's
    total assets. The lake has no taxes-payable line, so ``dTP = 0``.
    Missing short-term debt counts as zero; any other missing input gives
    ``None``. Higher is worse (earnings made of accruals, not cash).
    """
    ca, ca0 = line(cur, "current_assets"), line(prev, "current_assets")
    cash, cash0 = (
        line(cur, "cash_and_equivalents", "cash"),
        line(prev, "cash_and_equivalents", "cash"),
    )
    cl, cl0 = line(cur, "current_liabilities"), line(prev, "current_liabilities")
    dep = depreciation(cur)
    if None in (ca, ca0, cash, cash0, cl, cl0, dep):
        return None
    d_std = _zero(cur, "short_term_debt") - _zero(prev, "short_term_debt")
    accruals = (ca - ca0) - (cash - cash0) - ((cl - cl0) - d_std) - dep  # type: ignore[operator]
    return _pos_div(accruals, line(cur, "total_assets"))


def snoa(bs: Row) -> float | None:
    """Scaled net operating assets (Hirshleifer et al. 2004):

    ``OA = TA - cash and short-term investments``;
    ``OL = TA - STD - LTD - minority interest - preferred - common equity``;
    ``SNOA = (OA - OL) / TA``.

    Absent debt, minority and preferred lines count as zero; total assets
    and equity are required. Higher is worse (a balance sheet bloated by
    past accruals).
    """
    ta = line(bs, "total_assets")
    equity = line(bs, "total_stockholder_equity")
    if ta is None or ta <= 0 or equity is None:
        return None
    oa = ta - _cash_and_investments(bs)
    ol = (
        ta
        - _zero(bs, "short_term_debt")
        - _long_term_debt(bs)
        - _zero(bs, "noncontrolling_interest")
        - _zero(bs, "preferred_stock_total_equity")
        - equity
    )
    return (oa - ol) / ta


_BENEISH_COEF = {
    "dsri": 0.920,
    "gmi": 0.528,
    "aqi": 0.404,
    "sgi": 0.892,
    "depi": 0.115,
    "sgai": -0.172,
    "tata": 4.679,
    "lvgi": -0.327,
}
_BENEISH_CONST = -4.84


def beneish_indices(cur: Row, prev: Row) -> dict[str, float] | None:
    """The eight Beneish (1999) indices, year ``t`` over ``t-1``:

    - ``DSRI = (REC_t/Sales_t) / (REC_t-1/Sales_t-1)``, days sales in receivables;
    - ``GMI = GM_t-1 / GM_t``, gross margin deterioration;
    - ``AQI = (1 - (CA_t + PPE_t)/TA_t) / (1 - (CA_t-1 + PPE_t-1)/TA_t-1)``, asset quality;
    - ``SGI = Sales_t / Sales_t-1``, sales growth;
    - ``DEPI = rate_t-1 / rate_t`` with ``rate = Dep / (Dep + PPE)``;
    - ``SGAI = (SGA_t/Sales_t) / (SGA_t-1/Sales_t-1)``;
    - ``LVGI = ((CL_t + LTD_t)/TA_t) / ((CL_t-1 + LTD_t-1)/TA_t-1)``;
    - ``TATA = (income from continuing operations_t - CFO_t) / TA_t``.

    An index whose inputs are missing (or whose ratio is undefined) is set
    to its neutral value, 1 (0 for TATA), as in Beneish, Lee & Nichols
    (2013). Positive sales in both years are required: ``None`` otherwise.
    """
    s1, s0 = line(cur, "revenue"), line(prev, "revenue")
    if s1 is None or s0 is None or s1 <= 0 or s0 <= 0:
        return None

    def over_sales(row: Row, name: str, sales: float) -> float | None:
        return _div(line(row, name), sales)

    def aq(row: Row) -> float | None:
        ta, ca, ppe = (
            line(row, "total_assets"),
            line(row, "current_assets"),
            line(row, "property_plant_equipment_net"),
        )
        if ta is None or ca is None or ppe is None or ta <= 0:
            return None
        return 1.0 - (ca + ppe) / ta

    def dep_rate(row: Row) -> float | None:
        dep, ppe = depreciation(row), line(row, "property_plant_equipment_net")
        return None if dep is None or ppe is None else _pos_div(dep, dep + ppe)

    def lev(row: Row) -> float | None:
        cl = line(row, "current_liabilities")
        return (
            None if cl is None else _pos_div(cl + _long_term_debt(row), line(row, "total_assets"))
        )

    def ratio(a: float | None, b: float | None) -> float:
        r = _div(a, b)
        return 1.0 if r is None or r <= 0 else r

    income = line(cur, "net_income_continuing", "net_income")
    cfo = line(cur, "operating_cash_flow")
    tata = (
        None if income is None or cfo is None else _pos_div(income - cfo, line(cur, "total_assets"))
    )
    return {
        "dsri": ratio(
            over_sales(cur, "net_receivables", s1), over_sales(prev, "net_receivables", s0)
        ),
        "gmi": ratio(gross_margin(prev), gross_margin(cur)),
        "aqi": ratio(aq(cur), aq(prev)),
        "sgi": s1 / s0,
        "depi": ratio(dep_rate(prev), dep_rate(cur)),
        "sgai": ratio(
            over_sales(cur, "selling_general_administrative", s1),
            over_sales(prev, "selling_general_administrative", s0),
        ),
        "lvgi": ratio(lev(cur), lev(prev)),
        "tata": 0.0 if tata is None else tata,
    }


def beneish_m(cur: Row, prev: Row) -> float | None:
    """Beneish (1999) eight-variable M-score:

    ``M = -4.84 + 0.920 DSRI + 0.528 GMI + 0.404 AQI + 0.892 SGI
    + 0.115 DEPI - 0.172 SGAI + 4.679 TATA - 0.327 LVGI``.

    An unchanged firm (every index 1, no accruals) scores -2.48; higher
    means likelier manipulation. See :func:`beneish_indices`."""
    idx = beneish_indices(cur, prev)
    if idx is None:
        return None
    return _BENEISH_CONST + sum(c * idx[k] for k, c in _BENEISH_COEF.items())


def pman(m: float | None) -> float | None:
    """Probability of manipulation, ``PMAN = Phi(M)`` (the standard normal
    CDF of the M-score; Beneish's probit)."""
    if m is None:
        return None
    return 0.5 * (1.0 + math.erf(m / math.sqrt(2.0)))


def altman_z(row: Row, market_cap: float | None) -> float | None:
    """Altman (1968) Z-score for public firms:

    ``Z = 1.2 X1 + 1.4 X2 + 3.3 X3 + 0.6 X4 + 1.0 X5`` with
    ``X1 = working capital / TA``, ``X2 = retained earnings / TA``,
    ``X3 = EBIT / TA``, ``X4 = market cap / total liabilities``,
    ``X5 = sales / TA``.

    Below 1.81 is Altman's distress zone. ``None`` when an input is
    missing or TA / total liabilities is not positive."""
    ta, tl = line(row, "total_assets"), line(row, "total_liabilities")
    ca, cl = line(row, "current_assets"), line(row, "current_liabilities")
    re, e, sales = line(row, "retained_earnings"), ebit(row), line(row, "revenue")
    if None in (ta, tl, ca, cl, re, e, sales, market_cap):
        return None
    if ta <= 0 or tl <= 0:  # type: ignore[operator]
        return None
    return (
        1.2 * (ca - cl) / ta  # type: ignore[operator]
        + 1.4 * re / ta  # type: ignore[operator]
        + 3.3 * e / ta  # type: ignore[operator]
        + 0.6 * market_cap / tl  # type: ignore[operator]
        + 1.0 * sales / ta  # type: ignore[operator]
    )


# ---- financial strength ------------------------------------------------------------


def _roa(row: Row) -> float | None:
    return _pos_div(line(row, "net_income"), line(row, "total_assets"))


def _cfo_ta(row: Row) -> float | None:
    return _pos_div(line(row, "operating_cash_flow"), line(row, "total_assets"))


def _fcf_ta(row: Row) -> float | None:
    return _pos_div(free_cash_flow(row), line(row, "total_assets"))


def _leverage(row: Row) -> float | None:
    return _pos_div(_long_term_debt(row), line(row, "total_assets"))


def _current_ratio(row: Row) -> float | None:
    return _pos_div(line(row, "current_assets"), line(row, "current_liabilities"))


def _turnover(row: Row) -> float | None:
    return _pos_div(line(row, "revenue"), line(row, "total_assets"))


def _shares(row: Row) -> float | None:
    return line(row, "common_stock_shares_outstanding")


def _gt(a: float | None, b: float | None) -> int:
    return int(a is not None and b is not None and a > b)


def _lever_fell(cur: Row, prev: Row) -> int:
    """Long-term debt / assets fell, or stayed at zero (no new leverage)."""
    lev, lev0 = _leverage(cur), _leverage(prev)
    if lev is None or lev0 is None:
        return 0
    return int(lev < lev0 or lev == lev0 == 0.0)


def _has_assets(cur: Row, prev: Row) -> bool:
    return all((line(r, "total_assets") or 0.0) > 0 for r in (cur, prev))


def piotroski_signals(cur: Row, prev: Row) -> dict[str, int]:
    """Piotroski's (2000) nine binary signals (1 = good news):

    - ``roa``: ROA = net income / assets > 0;
    - ``cfo``: operating cash flow > 0;
    - ``delta_roa``: ROA rose;
    - ``accrual``: CFO / assets > ROA;
    - ``delta_lever``: long-term debt / assets fell (or stayed at zero);
    - ``delta_liquid``: current ratio rose;
    - ``eq_offer``: shares outstanding did not rise (no equity issued);
    - ``delta_margin``: gross margin rose;
    - ``delta_turn``: asset turnover (sales / assets) rose.

    A signal whose inputs are missing scores 0: an unverifiable signal
    earns no point. Deviation: ratios use year-end assets, not
    beginning-of-year, so two years of statements suffice.
    """
    roa, roa0 = _roa(cur), _roa(prev)
    cfo = line(cur, "operating_cash_flow")
    return {
        "roa": _gt(roa, 0.0),
        "cfo": _gt(cfo, 0.0),
        "delta_roa": _gt(roa, roa0),
        "accrual": _gt(_cfo_ta(cur), roa),
        "delta_lever": _lever_fell(cur, prev),
        "delta_liquid": _gt(_current_ratio(cur), _current_ratio(prev)),
        "eq_offer": int(
            _shares(cur) is not None and _shares(prev) is not None and _shares(cur) <= _shares(prev)  # type: ignore[operator]
        ),
        "delta_margin": _gt(gross_margin(cur), gross_margin(prev)),
        "delta_turn": _gt(_turnover(cur), _turnover(prev)),
    }


def piotroski_f(cur: Row, prev: Row) -> int | None:
    """Piotroski F-score, 0-9: the sum of :func:`piotroski_signals`.
    ``None`` unless both years have positive total assets."""
    if not _has_assets(cur, prev):
        return None
    return sum(piotroski_signals(cur, prev).values())


def fs_signals(cur: Row, prev: Row) -> dict[str, int]:
    """Gray & Carlisle's ten financial-strength signals (1 = strong):

    - current profitability: ``roa`` (ROA > 0), ``fcfta`` (FCF / assets > 0),
      ``accrual`` (FCF / assets > ROA);
    - stability: ``lever`` (long-term debt / assets fell or stayed at
      zero), ``liquid`` (current ratio rose), ``neqiss`` (net buyback: the
      share count fell);
    - operating improvement: ``delta_roa``, ``delta_fcfta``,
      ``delta_margin`` (gross margin), ``delta_turn`` (asset turnover) rose.

    Missing inputs score 0. Net equity issuance is measured by the share
    count because vendors sign buyback and issuance cash flows differently.
    """
    roa, roa0 = _roa(cur), _roa(prev)
    fcfta, fcfta0 = _fcf_ta(cur), _fcf_ta(prev)
    return {
        "roa": _gt(roa, 0.0),
        "fcfta": _gt(fcfta, 0.0),
        "accrual": _gt(fcfta, roa),
        "lever": _lever_fell(cur, prev),
        "liquid": _gt(_current_ratio(cur), _current_ratio(prev)),
        "neqiss": _gt(_shares(prev), _shares(cur)),
        "delta_roa": _gt(roa, roa0),
        "delta_fcfta": _gt(fcfta, fcfta0),
        "delta_margin": _gt(gross_margin(cur), gross_margin(prev)),
        "delta_turn": _gt(_turnover(cur), _turnover(prev)),
    }


def fs_score(cur: Row, prev: Row) -> int | None:
    """Financial strength, 0-10: the sum of :func:`fs_signals`. ``None``
    unless both years have positive total assets."""
    if not _has_assets(cur, prev):
        return None
    return sum(fs_signals(cur, prev).values())


# ---- franchise power ---------------------------------------------------------------


def geometric_mean_return(returns: Sequence[float]) -> float | None:
    """``(prod(1 + r))^(1/n) - 1``; ``None`` for no returns or any ``r <= -1``."""
    if not returns or any(r <= -1.0 for r in returns):
        return None
    return float(np.exp(np.mean(np.log1p(np.asarray(returns, dtype=float)))) - 1.0)


def margin_growth(margins: Sequence[float]) -> float | None:
    """Compound annual growth of gross margin,
    ``(GM_last / GM_first)^(1/(n-1)) - 1``; needs two positive end points."""
    if len(margins) < 2 or margins[0] <= 0 or margins[-1] <= 0:
        return None
    return (margins[-1] / margins[0]) ** (1.0 / (len(margins) - 1)) - 1.0


def margin_stability(margins: Sequence[float]) -> float | None:
    """``mean(GM) / std(GM)`` (population std), capped at
    :data:`MAX_MARGIN_STABILITY`; needs two margins and a positive mean."""
    if len(margins) < 2:
        return None
    arr = np.asarray(margins, dtype=float)
    mean, std = float(arr.mean()), float(arr.std())
    if mean <= 0:
        return None
    if std <= mean / MAX_MARGIN_STABILITY:
        return MAX_MARGIN_STABILITY
    return mean / std


def franchise_inputs(years: Sequence[Row], *, min_years: int = 3) -> dict[str, Any] | None:
    """Per-firm inputs to franchise power from annual rows, oldest first
    (Gray & Carlisle use eight years; we use what is given):

    - ``roa``: geometric mean of net income / total assets;
    - ``roc``: geometric mean of :func:`roc`;
    - ``cfoa``: sum of free cash flow / latest total assets;
    - ``margin_growth`` and ``margin_stability`` of gross margin.

    Each series needs ``min_years`` usable years, else it is ``None``;
    fewer than ``min_years`` rows returns ``None``. ``years_used`` is the
    number of rows given.
    """
    if len(years) < max(min_years, 1):
        return None

    def series(fn) -> list[float]:
        return [v for v in (fn(y) for y in years) if v is not None]

    roas = series(_roa)
    rocs = series(lambda y: roc(ebit(y), y))
    fcfs = series(free_cash_flow)
    margins = series(gross_margin)
    enough = len(margins) >= min_years
    return {
        "years_used": len(years),
        "roa": geometric_mean_return(roas) if len(roas) >= min_years else None,
        "roc": geometric_mean_return(rocs) if len(rocs) >= min_years else None,
        "cfoa": _pos_div(sum(fcfs), line(years[-1], "total_assets"))
        if len(fcfs) >= min_years
        else None,
        "margin_growth": margin_growth(margins) if enough else None,
        "margin_stability": margin_stability(margins) if enough else None,
    }


def pct_rank(s: pd.Series) -> pd.Series:
    """Cross-sectional percentile rank in ``(0, 1]``, higher value = higher
    rank; ``NaN`` stays ``NaN`` and doesn't count."""
    return pd.to_numeric(s, errors="coerce").rank(pct=True, method="average")


def franchise_power(frame: pd.DataFrame) -> pd.Series:
    """Cross-sectional franchise power (Gray & Carlisle): the percentile of
    the mean of ``pct(roa)``, ``pct(roc)``, ``pct(cfoa)`` and
    ``max(pct(margin_growth), pct(margin_stability))``. Missing components
    are skipped; a firm with none is ``NaN``."""
    pr = {c: pct_rank(frame[c]) for c in frame.columns}
    margin = pd.concat([pr["margin_growth"], pr["margin_stability"]], axis=1).max(axis=1)
    parts = pd.concat([pr["roa"], pr["roc"], pr["cfoa"], margin], axis=1)
    return pct_rank(parts.mean(axis=1, skipna=True))
