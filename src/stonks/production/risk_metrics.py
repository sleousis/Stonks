"""Live VaR and expected shortfall with a violation ratio and a Kupiec test
(BL-47, roadmap 9.5.4; Danielsson, *Financial Risk Forecasting*).

After every real tick, for each portfolio and for each strategy's sleeve of
it (the positions ``position_attribution`` gives the strategy):

1. **Forecast.** A one-day 95% and 99% VaR and ES from an EWMA covariance
   (RiskMetrics, ``lam`` 0.94) of the last ``window`` (250) daily returns of
   the held names, normal quantiles, zero mean. Figures are fractions of the
   book's value and a loss is positive. A portfolio's value includes its
   cash, so cash lowers its VaR. A sleeve's value is its gross exposure.
2. **Backtest.** The day's hypothetical return: yesterday's exposures times
   each name's adjusted return since yesterday's snapshot, over yesterday's
   value. Trades, deposits and fees do not move it. A loss beyond
   yesterday's VaR is a violation.
3. **Scores.** Over the last ``window`` scored days: the violation ratio
   (violations over the expected count, 1.0 is right, above 1.5 or below 0.5
   means the model is wrong) and the Kupiec proportion-of-failures p-value.

One row per book per day goes to ``risk_snapshots`` (migration 019). A rerun
of the same day replaces it. Returns mix calendars by carrying the last
close forward: a weekend move of a crypto name lands on Monday.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
from scipy.stats import chi2, norm

from stonks.logging import get_logger
from stonks.production.decay import DecaySettings, evaluate_decay, expected_ir
from stonks.production.monitor_settings import RiskMonitorSettings
from stonks.production.prices import load_history
from stonks.store.state import SqliteState

__all__ = [
    "LEVELS",
    "PORTFOLIO_BOOK",
    "BookState",
    "RiskAlert",
    "RiskForecast",
    "RiskMonitorSettings",
    "RiskSnapshot",
    "book_forecast",
    "ewma_sigma",
    "kupiec_pof",
    "latest_portfolio_rows",
    "latest_snapshots",
    "list_snapshots",
    "parametric_var_es",
    "record_risk_snapshots",
    "risk_snapshots_enabled",
    "score_book",
    "violation_ratio",
    "write_snapshot",
]

_log = get_logger("stonks.production.risk_metrics")

#: The two confidence levels every snapshot carries.
LEVELS = (0.95, 0.99)
#: ``strategy_id`` of a whole portfolio's row (a sleeve row names its strategy).
PORTFOLIO_BOOK = ""


# ---- the math ----------------------------------------------------------------------


@dataclass(frozen=True)
class RiskForecast:
    """One-day forecast as fractions of the book's value (loss positive)."""

    sigma: float
    var_95: float
    var_99: float
    es_95: float
    es_99: float
    #: Daily return rows behind the covariance.
    observations: int


def parametric_var_es(sigma: float, level: float) -> tuple[float, float]:
    """Normal, zero-mean one-day ``(VaR, ES)`` at ``level`` for a return
    with standard deviation ``sigma``."""
    if not 0.0 < level < 1.0:
        raise ValueError(f"level must lie in (0, 1), got {level}")
    z = float(norm.ppf(level))
    return sigma * z, sigma * float(norm.pdf(z)) / (1.0 - level)


def ewma_sigma(returns: pd.DataFrame, weights: Mapping[str, float], lam: float = 0.94) -> float:
    """Standard deviation of ``sum_i w_i r_i`` under the EWMA covariance of
    ``returns`` (columns are tickers). Names without a column or weight are
    left out; no weight at all is zero risk."""
    from stonks.portfolio.covariance import get_estimator

    names = [t for t, w in weights.items() if w != 0 and t in returns.columns]
    if not names or returns.empty:
        return 0.0
    w = np.array([float(weights[t]) for t in names])
    cov = get_estimator("ewma", lam=lam).estimate(returns[names].to_numpy(dtype=float))
    return float(math.sqrt(max(float(w @ cov @ w), 0.0)))


def _returns(closes: Mapping[str, pd.Series], names: Sequence[str], window: int) -> pd.DataFrame:
    frame = pd.DataFrame({t: closes[t] for t in names if t in closes}).sort_index().ffill()
    return frame.pct_change(fill_method=None).iloc[1:].dropna().tail(window)


def book_forecast(
    closes: Mapping[str, pd.Series],
    exposures: Mapping[str, float],
    value: float,
    *,
    lam: float = 0.94,
    window: int = 250,
) -> RiskForecast | None:
    """The forecast of a book holding ``exposures`` (currency per ticker)
    worth ``value`` in all. ``None`` for a book worth nothing."""
    if not value > 0:
        return None
    names = [t for t, e in exposures.items() if e != 0]
    returns = _returns(closes, names, window)
    weights = {t: exposures[t] / value for t in names}
    sigma = ewma_sigma(returns, weights, lam=lam)
    var95, es95 = parametric_var_es(sigma, 0.95)
    var99, es99 = parametric_var_es(sigma, 0.99)
    return RiskForecast(sigma, var95, var99, es95, es99, int(len(returns)) if names else 0)


def violation_ratio(violations: int, observations: int, p: float) -> float | None:
    """Observed over expected violations (1.0 is a well-calibrated model)."""
    if observations <= 0:
        return None
    return violations / (p * observations)


def kupiec_pof(violations: int, observations: int, p: float) -> float | None:
    """Kupiec's proportion-of-failures test: the chi-square(1) p-value of
    the likelihood ratio of the observed violation rate against ``p``."""
    if observations <= 0:
        return None
    x, t = violations, observations
    pi = x / t

    def loglik(q: float) -> float:
        # 0 * log(0) is 0 in the likelihood
        out = 0.0
        if t - x:
            out += (t - x) * math.log(1.0 - q) if q < 1.0 else -math.inf
        if x:
            out += x * math.log(q) if q > 0.0 else -math.inf
        return out

    lr = max(-2.0 * (loglik(p) - loglik(pi)), 0.0)
    return float(chi2.sf(lr, 1))


# ---- one book per day --------------------------------------------------------------


@dataclass(frozen=True)
class BookState:
    """What the tick left in one book on ``as_of``."""

    portfolio_id: str
    #: ``PORTFOLIO_BOOK`` for the whole portfolio, else the strategy id.
    strategy_id: str
    as_of: date
    value: float
    #: Currency exposure per ticker at the day's marks.
    exposures: Mapping[str, float]


@dataclass(frozen=True)
class RiskSnapshot:
    portfolio_id: str
    strategy_id: str
    as_of: date
    tick_id: str | None
    value: float
    exposures: Mapping[str, float]
    sigma: float | None
    var_95: float | None
    var_99: float | None
    es_95: float | None
    es_99: float | None
    observations: int
    realized_return: float | None
    pnl: float | None
    violation_95: bool | None
    violation_99: bool | None
    window_days: int
    violations_95: int
    violations_99: int
    violation_ratio_95: float | None
    violation_ratio_99: float | None
    kupiec_p_95: float | None
    kupiec_p_99: float | None
    ir_short: float | None = None
    ir_long: float | None = None
    expected_ir: float | None = None
    decay_days: int | None = None
    decayed: bool = False
    decay_reason: str | None = None
    created_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["as_of"] = self.as_of.isoformat()
        out["exposures"] = dict(self.exposures)
        out["strategy_id"] = self.strategy_id or None
        return out

    def ratio_out_of_band(self, settings: RiskMonitorSettings) -> bool:
        """The 95% violation ratio is judged and outside the band."""
        r = self.violation_ratio_95
        return (
            self.window_days >= settings.min_window
            and r is not None
            and not settings.ratio_low <= r <= settings.ratio_high
        )


def risk_snapshots_enabled(state: SqliteState) -> bool:
    rows = state.sql("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'risk_snapshots'")
    return bool(rows)


def _day_return(closes: Mapping[str, pd.Series], ticker: str, prev: date, day: date) -> float:
    s = closes.get(ticker)
    if s is None or s.empty:
        return 0.0
    idx = np.array([pd.Timestamp(x).date() for x in s.index])
    before = s.loc[idx <= prev]
    after = s.loc[idx <= day]
    if before.empty or after.empty or not before.iloc[-1] > 0:
        return 0.0
    return float(after.iloc[-1] / before.iloc[-1] - 1.0)


def score_book(
    state: SqliteState,
    book: BookState,
    closes: Mapping[str, pd.Series],
    settings: RiskMonitorSettings,
    *,
    tick_id: str | None = None,
) -> RiskSnapshot:
    """Today's forecast for ``book``, yesterday's forecast scored against
    today's hypothetical return, and the rolling violation scores."""
    forecast = book_forecast(
        closes, book.exposures, book.value, lam=settings.lam, window=settings.window
    )
    prev = _previous(state, book)
    realized = pnl = None
    v95 = v99 = None
    if prev is not None and prev["value"] and prev["value"] > 0:
        prev_day = date.fromisoformat(prev["as_of"])
        exposures = json.loads(prev["exposures_json"] or "{}")
        pnl = sum(
            float(e) * _day_return(closes, t, prev_day, book.as_of) for t, e in exposures.items()
        )
        realized = pnl / float(prev["value"])
        if prev["var_95"] is not None:
            v95 = realized < -float(prev["var_95"])
        if prev["var_99"] is not None:
            v99 = realized < -float(prev["var_99"])
    history = _scored_history(state, book, settings.window - (0 if v95 is None else 1))
    flags95 = [bool(r["violation_95"]) for r in history] + ([] if v95 is None else [v95])
    flags99 = [bool(r["violation_99"]) for r in history if r["violation_99"] is not None]
    if v99 is not None:
        flags99.append(v99)
    n = len(flags95)
    x95, x99 = sum(flags95), sum(flags99)
    return RiskSnapshot(
        portfolio_id=book.portfolio_id,
        strategy_id=book.strategy_id,
        as_of=book.as_of,
        tick_id=tick_id,
        value=float(book.value),
        exposures={t: float(e) for t, e in book.exposures.items() if e != 0},
        sigma=forecast.sigma if forecast else None,
        var_95=forecast.var_95 if forecast else None,
        var_99=forecast.var_99 if forecast else None,
        es_95=forecast.es_95 if forecast else None,
        es_99=forecast.es_99 if forecast else None,
        observations=forecast.observations if forecast else 0,
        realized_return=realized,
        pnl=pnl,
        violation_95=v95,
        violation_99=v99,
        window_days=n,
        violations_95=x95,
        violations_99=x99,
        violation_ratio_95=violation_ratio(x95, n, 0.05),
        violation_ratio_99=violation_ratio(x99, len(flags99), 0.01),
        kupiec_p_95=kupiec_pof(x95, n, 0.05),
        kupiec_p_99=kupiec_pof(x99, len(flags99), 0.01),
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )


def _previous(state: SqliteState, book: BookState) -> Any:
    rows = state.sql(
        "SELECT as_of, value, exposures_json, var_95, var_99 FROM risk_snapshots"
        " WHERE portfolio_id = ? AND strategy_id = ? AND as_of < ?"
        " ORDER BY as_of DESC LIMIT 1",
        [book.portfolio_id, book.strategy_id, book.as_of.isoformat()],
    )
    return rows[0] if rows else None


def _scored_history(state: SqliteState, book: BookState, limit: int) -> list[Any]:
    """The last ``limit`` scored days before ``book.as_of``, oldest first
    (``limit`` is the window or one less, at least 19)."""
    rows = state.sql(
        "SELECT violation_95, violation_99 FROM risk_snapshots"
        " WHERE portfolio_id = ? AND strategy_id = ? AND as_of < ?"
        " AND violation_95 IS NOT NULL ORDER BY as_of DESC LIMIT ?",
        [book.portfolio_id, book.strategy_id, book.as_of.isoformat(), limit],
    )
    return list(reversed(rows))


_COLUMNS = (
    "as_of",
    "tick_id",
    "portfolio_id",
    "strategy_id",
    "value",
    "exposures_json",
    "sigma",
    "var_95",
    "var_99",
    "es_95",
    "es_99",
    "observations",
    "realized_return",
    "pnl",
    "violation_95",
    "violation_99",
    "window_days",
    "violations_95",
    "violations_99",
    "violation_ratio_95",
    "violation_ratio_99",
    "kupiec_p_95",
    "kupiec_p_99",
    "ir_short",
    "ir_long",
    "expected_ir",
    "decay_days",
    "decayed",
    "decay_reason",
    "created_at",
)


def _flag(value: bool | None) -> int | None:
    return None if value is None else int(value)


def write_snapshot(state: SqliteState, snap: RiskSnapshot) -> None:
    """Insert ``snap``, replacing the book's row for the same day."""
    values = [
        snap.as_of.isoformat(),
        snap.tick_id,
        snap.portfolio_id,
        snap.strategy_id,
        snap.value,
        json.dumps(dict(sorted(snap.exposures.items()))),
        snap.sigma,
        snap.var_95,
        snap.var_99,
        snap.es_95,
        snap.es_99,
        snap.observations,
        snap.realized_return,
        snap.pnl,
        _flag(snap.violation_95),
        _flag(snap.violation_99),
        snap.window_days,
        snap.violations_95,
        snap.violations_99,
        snap.violation_ratio_95,
        snap.violation_ratio_99,
        snap.kupiec_p_95,
        snap.kupiec_p_99,
        snap.ir_short,
        snap.ir_long,
        snap.expected_ir,
        snap.decay_days,
        int(snap.decayed),
        snap.decay_reason,
        snap.created_at or datetime.now(UTC).isoformat(timespec="seconds"),
    ]
    marks = ", ".join("?" for _ in _COLUMNS)
    updates = ", ".join(
        f"{c} = excluded.{c}" for c in _COLUMNS if c not in ("as_of", "portfolio_id", "strategy_id")
    )
    state.execute(
        f"INSERT INTO risk_snapshots ({', '.join(_COLUMNS)}) VALUES ({marks})"
        f" ON CONFLICT (portfolio_id, strategy_id, as_of) DO UPDATE SET {updates}",
        values,
    )


# ---- reads -------------------------------------------------------------------------


def _row_snapshot(row: Any) -> RiskSnapshot:
    def flag(v: Any) -> bool | None:
        return None if v is None else bool(v)

    return RiskSnapshot(
        portfolio_id=row["portfolio_id"],
        strategy_id=row["strategy_id"],
        as_of=date.fromisoformat(row["as_of"]),
        tick_id=row["tick_id"],
        value=float(row["value"]),
        exposures=json.loads(row["exposures_json"] or "{}"),
        sigma=row["sigma"],
        var_95=row["var_95"],
        var_99=row["var_99"],
        es_95=row["es_95"],
        es_99=row["es_99"],
        observations=int(row["observations"]),
        realized_return=row["realized_return"],
        pnl=row["pnl"],
        violation_95=flag(row["violation_95"]),
        violation_99=flag(row["violation_99"]),
        window_days=int(row["window_days"]),
        violations_95=int(row["violations_95"]),
        violations_99=int(row["violations_99"]),
        violation_ratio_95=row["violation_ratio_95"],
        violation_ratio_99=row["violation_ratio_99"],
        kupiec_p_95=row["kupiec_p_95"],
        kupiec_p_99=row["kupiec_p_99"],
        ir_short=row["ir_short"],
        ir_long=row["ir_long"],
        expected_ir=row["expected_ir"],
        decay_days=row["decay_days"],
        decayed=bool(row["decayed"]),
        decay_reason=row["decay_reason"],
        created_at=row["created_at"],
    )


def list_snapshots(
    state: SqliteState,
    portfolio_id: str,
    *,
    strategy_id: str | None = None,
    since: date | None = None,
    limit: int = 250,
    offset: int = 0,
) -> tuple[list[RiskSnapshot], int]:
    """One portfolio's rows, newest first: the whole book only
    (``strategy_id=PORTFOLIO_BOOK``), one strategy's sleeve, or every row
    (``None``). Returns the page and the total count."""
    where = ["portfolio_id = ?"]
    params: list[Any] = [portfolio_id]
    if strategy_id is not None:
        where.append("strategy_id = ?")
        params.append(strategy_id)
    if since is not None:
        where.append("as_of >= ?")
        params.append(since.isoformat())
    clause = " AND ".join(where)
    total = state.sql(f"SELECT COUNT(*) FROM risk_snapshots WHERE {clause}", params)[0][0]
    rows = state.sql(
        f"SELECT * FROM risk_snapshots WHERE {clause}"
        " ORDER BY as_of DESC, strategy_id LIMIT ? OFFSET ?",
        [*params, limit, offset],
    )
    return [_row_snapshot(r) for r in rows], int(total)


def latest_snapshots(state: SqliteState, portfolio_id: str) -> list[RiskSnapshot]:
    """Every book of ``portfolio_id`` on its latest snapshot day, the whole
    portfolio first."""
    rows = state.sql(
        "SELECT * FROM risk_snapshots WHERE portfolio_id = ? AND as_of = ("
        " SELECT MAX(as_of) FROM risk_snapshots WHERE portfolio_id = ?)"
        " ORDER BY strategy_id",
        [portfolio_id, portfolio_id],
    )
    return [_row_snapshot(r) for r in rows]


def latest_portfolio_rows(state: SqliteState, on: date | None = None) -> list[RiskSnapshot]:
    """Each portfolio's latest whole-book row (on or before ``on``)."""
    cutoff = (on or date.max - timedelta(days=1)).isoformat()
    rows = state.sql(
        "SELECT r.* FROM risk_snapshots r JOIN ("
        " SELECT portfolio_id, MAX(as_of) AS as_of FROM risk_snapshots"
        " WHERE strategy_id = '' AND as_of <= ? GROUP BY portfolio_id) m"
        " ON m.portfolio_id = r.portfolio_id AND m.as_of = r.as_of"
        " WHERE r.strategy_id = '' ORDER BY r.portfolio_id",
        [cutoff],
    )
    return [_row_snapshot(r) for r in rows]


# ---- the daily run -----------------------------------------------------------------

#: ``publish(event)``: the notification router's publish (tests pass a spy).
Publish = Callable[[Any], Any]


@dataclass(frozen=True)
class RiskAlert:
    kind: str  # "var_violations" | "alpha_decay"
    portfolio_id: str
    strategy_id: str | None
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _marks(lake: Any, tickers: Sequence[str], as_of: date) -> dict[str, float]:
    """The latest raw daily close at or before ``as_of`` per ticker, in the
    major currency unit (pounds, not pence), as the book's cash is."""
    from stonks.fx.units import price_scales

    if not tickers:
        return {}
    df = lake.sql(
        "SELECT ticker, close FROM ("
        " SELECT ticker, close, row_number() OVER (PARTITION BY ticker ORDER BY date DESC) AS rn"
        " FROM prices WHERE ticker = ANY(?) AND date <= ?) WHERE rn = 1",
        [sorted(set(tickers)), as_of],
    )
    scales = price_scales(lake, tickers)
    return {
        str(r.ticker): float(r.close) * scales.get(str(r.ticker), 1.0)
        for r in df.itertuples(index=False)
        if r.close
    }


def _portfolio_positions(state: SqliteState, portfolio_id: str, as_of: date) -> Any:
    rows = state.sql(
        "SELECT cash, positions_json FROM portfolio_snapshots"
        " WHERE portfolio_id = ? AND as_of = ? ORDER BY id DESC LIMIT 1",
        [portfolio_id, as_of.isoformat()],
    )
    return rows[0] if rows else None


def _sleeves(state: SqliteState, portfolio_id: str, as_of: date) -> dict[str, dict[str, float]]:
    """Shares held per strategy per ticker on ``as_of`` (quantity times share)."""
    exists = state.sql(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'position_attribution'"
    )
    if not exists:
        return {}
    rows = state.sql(
        "SELECT strategy_id, ticker, quantity, weight_share FROM position_attribution"
        " WHERE portfolio_id = ? AND as_of = ?",
        [portfolio_id, as_of.isoformat()],
    )
    out: dict[str, dict[str, float]] = {}
    for r in rows:
        held = float(r["quantity"]) * float(r["weight_share"])
        if held:
            book = out.setdefault(r["strategy_id"], {})
            book[r["ticker"]] = book.get(r["ticker"], 0.0) + held
    return out


def _previous_tickers(state: SqliteState, portfolio_id: str, as_of: date) -> set[str]:
    rows = state.sql(
        "SELECT exposures_json FROM risk_snapshots WHERE portfolio_id = ? AND as_of = ("
        " SELECT MAX(as_of) FROM risk_snapshots WHERE portfolio_id = ? AND as_of < ?)",
        [portfolio_id, portfolio_id, as_of.isoformat()],
    )
    return {t for r in rows for t in json.loads(r["exposures_json"] or "{}")}


def _latest_metrics(state: SqliteState, strategy_id: str) -> dict[str, dict[str, Any]]:
    rows = state.sql(
        "SELECT test_id, metrics_json FROM survival_reports WHERE strategy_id = ? ORDER BY id",
        [strategy_id],
    )
    return {r["test_id"]: json.loads(r["metrics_json"] or "{}") for r in rows}


def _with_decay(
    state: SqliteState, snap: RiskSnapshot, settings: DecaySettings
) -> tuple[RiskSnapshot, bool]:
    """``snap`` with the decay columns, and whether the monitor newly fired."""
    rows = state.sql(
        "SELECT realized_return, decayed FROM risk_snapshots"
        " WHERE portfolio_id = ? AND strategy_id = ? AND as_of < ?"
        " ORDER BY as_of DESC LIMIT ?",
        [snap.portfolio_id, snap.strategy_id, snap.as_of.isoformat(), settings.long_window * 2],
    )
    history = [
        float(r["realized_return"]) for r in reversed(rows) if r["realized_return"] is not None
    ]
    if snap.realized_return is not None:
        history.append(snap.realized_return)
    check = evaluate_decay(history, expected_ir(_latest_metrics(state, snap.strategy_id)), settings)
    was_decayed = bool(rows and rows[0]["decayed"])
    snap = replace(
        snap,
        ir_short=check.ir_short,
        ir_long=check.ir_long,
        expected_ir=check.expected_ir,
        decay_days=check.days_negative,
        decayed=check.decayed,
        decay_reason=check.reason,
    )
    return snap, check.decayed and not was_decayed


def record_risk_snapshots(
    state: SqliteState,
    lake: Any,
    as_of: date,
    portfolio_ids: Sequence[str],
    *,
    tick_id: str | None = None,
    settings: RiskMonitorSettings | None = None,
    decay: DecaySettings | None = None,
    publish: Publish | None = None,
) -> tuple[list[RiskSnapshot], list[RiskAlert]]:
    """Score and store every book of ``portfolio_ids`` that has a snapshot
    on ``as_of``, then alert the portfolio's owner: a violation ratio
    outside the band, or a sleeve whose alpha newly decayed. The router
    deduplicates alerts, and a failed send is logged, never raised."""
    settings = settings or RiskMonitorSettings()
    decay = decay or DecaySettings()
    snaps: list[RiskSnapshot] = []
    alerts: list[RiskAlert] = []
    if not settings.enabled or not risk_snapshots_enabled(state):
        return snaps, alerts
    for pid in portfolio_ids:
        row = _portfolio_positions(state, pid, as_of)
        if row is None:
            continue
        positions = {t: float(q) for t, q in json.loads(row["positions_json"] or "{}").items()}
        sleeves = _sleeves(state, pid, as_of)
        tickers = set(positions) | {t for s in sleeves.values() for t in s}
        tickers |= _previous_tickers(state, pid, as_of)
        marks = _marks(lake, sorted(tickers), as_of)
        history = load_history(lake, sorted(tickers), as_of, bars=settings.window + 10)
        closes = {t: pd.Series(f["close"]) for t, f in history.items() if "close" in f}

        exposures = {t: q * marks[t] for t, q in positions.items() if q and t in marks}
        value = float(row["cash"]) + sum(exposures.values())
        books = [BookState(pid, PORTFOLIO_BOOK, as_of, value, exposures)]
        for sid, held in sorted(sleeves.items()):
            sleeve = {t: q * marks[t] for t, q in held.items() if t in marks}
            books.append(BookState(pid, sid, as_of, sum(abs(e) for e in sleeve.values()), sleeve))

        with state.transaction():
            for book in books:
                snap = score_book(state, book, closes, settings, tick_id=tick_id)
                fired = False
                if book.strategy_id != PORTFOLIO_BOOK:
                    snap, fired = _with_decay(state, snap, decay)
                write_snapshot(state, snap)
                snaps.append(snap)
                if snap.ratio_out_of_band(settings) and not _was_out_of_band(state, book, settings):
                    alerts.append(_var_alert(snap))
                if fired:
                    alerts.append(
                        RiskAlert("alpha_decay", pid, snap.strategy_id, snap.decay_reason or "")
                    )
    for alert in alerts:
        _publish(state, alert, as_of, publish)
    return snaps, alerts


def _was_out_of_band(state: SqliteState, book: BookState, settings: RiskMonitorSettings) -> bool:
    """The book's previous day was already out of band (it alerted then)."""
    rows = state.sql(
        "SELECT window_days, violation_ratio_95 FROM risk_snapshots"
        " WHERE portfolio_id = ? AND strategy_id = ? AND as_of < ?"
        " ORDER BY as_of DESC LIMIT 1",
        [book.portfolio_id, book.strategy_id, book.as_of.isoformat()],
    )
    if not rows or rows[0]["violation_ratio_95"] is None:
        return False
    ratio = float(rows[0]["violation_ratio_95"])
    return int(rows[0]["window_days"]) >= settings.min_window and not (
        settings.ratio_low <= ratio <= settings.ratio_high
    )


def _var_alert(snap: RiskSnapshot) -> RiskAlert:
    ratio = snap.violation_ratio_95 or 0.0
    what = "too many" if ratio > 1 else "too few"
    return RiskAlert(
        "var_violations",
        snap.portfolio_id,
        snap.strategy_id or None,
        f"{what} 95% VaR violations: {snap.violations_95} in {snap.window_days} days"
        f" (ratio {ratio:.2f}, Kupiec p {snap.kupiec_p_95 or 0.0:.3f})",
    )


def _publish(state: SqliteState, alert: RiskAlert, as_of: date, publish: Publish | None) -> None:
    from stonks.notify.events import Audience, Event
    from stonks.notify.router import configured_router

    if alert.kind == "alpha_decay":
        title = f"Alpha decay: {alert.strategy_id} is below its backtest"
    else:
        title = f"VaR model off for {alert.strategy_id or 'the portfolio'}"
    event = Event(
        category="risk",
        level="warning",
        title=title,
        body=alert.detail,
        audience=Audience.owner_of(alert.portfolio_id),
        dedupe_key=f"{alert.kind}:{alert.portfolio_id}:{alert.strategy_id or ''}:{as_of}",
        deep_link="/health",
        strategy_id=alert.strategy_id,
        portfolio_id=alert.portfolio_id,
    )
    _log.warning("risk_monitor.alert", **alert.as_dict())
    try:
        (publish or configured_router(state).publish)(event)
    except Exception as exc:
        _log.error("risk_monitor.notify_failed", kind=alert.kind, error=str(exc))
