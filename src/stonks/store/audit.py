"""Statement audit (BL-36): set-based accounting checks over the lake.

The three statements are one system (Ittelson; Tracy), so a vendor row that
breaks an accounting identity is bad data, not a signal. Each
:class:`StatementCheck` is one DuckDB query returning the periods that fail
it. :func:`audit_statements` runs them all and replaces the audited tickers'
rows in ``statement_flags`` (DuckDB migration 014), so re-running is
idempotent and a fixed row loses its flag. Statement rows are never changed.

A check only fires when every line item it needs is present: a bank with no
``cost_of_revenue`` is not flagged for gross profit. Relative gaps divide by
the absolute value of the reference figure, and a zero reference skips the
row.

Checks (tolerances in :class:`AuditTolerances`):

* ``balance_identity`` (error): total assets differ from total liabilities
  plus equity, both with and without non-controlling interest.
* ``net_income_mismatch``: income-statement vs cash-flow net income.
* ``cash_mismatch``: cash-flow end-of-period cash vs balance-sheet cash.
* ``gross_profit_mismatch``: gross profit vs revenue minus cost of revenue.
* ``quarterly_sum_mismatch``: four quarters of revenue or net income vs the
  annual figure.
* ``filing_before_period_end`` (error): a filing date before the period
  closed, in any statement.
* ``negative_shares`` (error): negative shares outstanding.

Readers opt in to skipping flagged periods
(``DuckDBLake.get_statements_as_of(..., exclude_flagged=True)``).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

import pandas as pd

from stonks.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from stonks.store.lake import DuckDBLake

Severity = Literal["error", "warning"]

_log = get_logger("stonks.store.audit")

#: Name of the scope view a check filters on when the audit is limited to
#: some tickers.
_SCOPE = "_audit_scope"


@dataclass(frozen=True)
class AuditTolerances:
    """Relative gaps above which a check flags a period."""

    balance: float = 0.02
    net_income: float = 0.05
    cash: float = 0.05
    gross_profit: float = 0.02
    quarterly_sum: float = 0.05


@dataclass(frozen=True)
class StatementCheck:
    """One accounting check: ``sql`` selects ``ticker, period_end,
    frequency, detail`` for each failing period. ``{scope}`` in the SQL is
    replaced by a ticker filter on the alias ``s``."""

    check_id: str
    severity: Severity
    description: str
    sql: str


def _rel(a: str, b: str, ref: str) -> str:
    return f"ABS(({a}) - ({b})) / NULLIF(ABS({ref}), 0)"


def build_checks(tol: AuditTolerances | None = None) -> tuple[StatementCheck, ...]:
    """The audit's checks at the given tolerances."""
    t = tol or AuditTolerances()
    tle = "s.total_liabilities + s.total_stockholder_equity"
    tle_nci = f"{tle} + COALESCE(s.noncontrolling_interest, 0)"
    return (
        StatementCheck(
            "balance_identity",
            "error",
            "total assets differ from liabilities plus equity",
            f"""
            SELECT s.ticker, s.period_end, s.frequency,
                   printf('total_assets %.6g vs liabilities + equity %.6g',
                          s.total_assets, {tle_nci}) AS detail
              FROM balance_sheet s
             WHERE s.total_assets IS NOT NULL AND s.total_liabilities IS NOT NULL
               AND s.total_stockholder_equity IS NOT NULL {{scope}}
               AND {_rel("s.total_assets", tle_nci, "s.total_assets")} > {t.balance}
               AND {_rel("s.total_assets", tle, "s.total_assets")} > {t.balance}
            """,
        ),
        StatementCheck(
            "net_income_mismatch",
            "warning",
            "net income differs between the income and cash-flow statements",
            f"""
            SELECT s.ticker, s.period_end, s.frequency,
                   printf('income statement %.6g vs cash flow %.6g',
                          s.net_income, c.net_income) AS detail
              FROM income_statement s
              JOIN cash_flow_statement c USING (ticker, period_end, frequency)
             WHERE s.net_income IS NOT NULL AND c.net_income IS NOT NULL {{scope}}
               AND {_rel("s.net_income", "c.net_income", "s.net_income")} > {t.net_income}
            """,
        ),
        StatementCheck(
            "cash_mismatch",
            "warning",
            "cash-flow end-of-period cash differs from balance-sheet cash",
            f"""
            SELECT s.ticker, s.period_end, s.frequency,
                   printf('cash flow end cash %.6g vs balance sheet cash %.6g',
                          c.end_period_cash_flow,
                          COALESCE(s.cash_and_equivalents, s.cash)) AS detail
              FROM balance_sheet s
              JOIN cash_flow_statement c USING (ticker, period_end, frequency)
             WHERE c.end_period_cash_flow IS NOT NULL
               AND COALESCE(s.cash_and_equivalents, s.cash) IS NOT NULL {{scope}}
               AND {
                _rel(
                    "c.end_period_cash_flow",
                    "COALESCE(s.cash_and_equivalents, s.cash)",
                    "COALESCE(s.cash_and_equivalents, s.cash)",
                )
            } > {t.cash}
            """,
        ),
        StatementCheck(
            "gross_profit_mismatch",
            "warning",
            "gross profit differs from revenue minus cost of revenue",
            f"""
            SELECT s.ticker, s.period_end, s.frequency,
                   printf('gross_profit %.6g vs revenue - cost_of_revenue %.6g',
                          s.gross_profit, s.revenue - s.cost_of_revenue) AS detail
              FROM income_statement s
             WHERE s.gross_profit IS NOT NULL AND s.revenue IS NOT NULL
               AND s.cost_of_revenue IS NOT NULL {{scope}}
               AND {_rel("s.gross_profit", "s.revenue - s.cost_of_revenue", "s.revenue")} > {
                t.gross_profit
            }
            """,
        ),
        StatementCheck(
            "quarterly_sum_mismatch",
            "warning",
            "four quarters do not sum to the annual figure",
            f"""
            WITH q AS (
                SELECT s.ticker, s.period_end, s.frequency,
                       SUM(x.revenue) AS q_revenue,
                       COUNT(x.revenue) AS n_revenue,
                       SUM(x.net_income) AS q_net_income,
                       COUNT(x.net_income) AS n_net_income,
                       ANY_VALUE(s.revenue) AS revenue,
                       ANY_VALUE(s.net_income) AS net_income
                  FROM income_statement s
                  JOIN income_statement x
                    ON x.ticker = s.ticker AND x.frequency = 'Q'
                   AND x.period_end > s.period_end - INTERVAL 11 MONTH
                   AND x.period_end <= s.period_end + INTERVAL 7 DAY
                 WHERE s.frequency = 'A' {{scope}}
                 GROUP BY s.ticker, s.period_end, s.frequency
            )
            SELECT ticker, period_end, frequency,
                   concat_ws(' | ',
                       CASE WHEN n_revenue = 4
                             AND {_rel("q_revenue", "revenue", "revenue")} > {t.quarterly_sum}
                            THEN printf('revenue quarters %.6g vs annual %.6g',
                                        q_revenue, revenue) END,
                       CASE WHEN n_net_income = 4
                             AND {_rel("q_net_income", "net_income", "net_income")} > {
                t.quarterly_sum
            }
                            THEN printf('net_income quarters %.6g vs annual %.6g',
                                        q_net_income, net_income) END
                   ) AS detail
              FROM q
             WHERE (n_revenue = 4
                    AND {_rel("q_revenue", "revenue", "revenue")} > {t.quarterly_sum})
                OR (n_net_income = 4
                    AND {_rel("q_net_income", "net_income", "net_income")} > {t.quarterly_sum})
            """,
        ),
        StatementCheck(
            "filing_before_period_end",
            "error",
            "filing date is before the period closed",
            " UNION ALL ".join(
                f"""
                SELECT s.ticker, s.period_end, s.frequency,
                       printf('{table} filed %s before period end %s',
                              s.filing_date, s.period_end) AS detail
                  FROM {table} s
                 WHERE s.filing_date < s.period_end {{scope}}
                """
                for table in ("income_statement", "balance_sheet", "cash_flow_statement")
            ),
        ),
        StatementCheck(
            "negative_shares",
            "error",
            "shares outstanding is negative",
            """
            SELECT s.ticker, s.period_end, s.frequency,
                   printf('shares outstanding %.6g', s.common_stock_shares_outstanding)
                       AS detail
              FROM balance_sheet s
             WHERE s.common_stock_shares_outstanding < 0 {scope}
            """,
        ),
    )


#: The default checks.
CHECKS: tuple[StatementCheck, ...] = build_checks()

FLAG_COLUMNS = (
    "ticker",
    "period_end",
    "frequency",
    "check_id",
    "severity",
    "detail",
    "flagged_at",
)


@dataclass
class AuditReport:
    """What one audit found: flags per check id and the flag rows."""

    tickers: list[str] | None
    counts: dict[str, int] = field(default_factory=dict)
    flags: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=list(FLAG_COLUMNS)))

    @property
    def n_flags(self) -> int:
        return len(self.flags)


def find_flags(
    lake: DuckDBLake,
    tickers: Sequence[str] | None = None,
    checks: Sequence[StatementCheck] = CHECKS,
) -> pd.DataFrame:
    """Every flag the checks raise, without writing anything."""
    scope = "" if tickers is None else f"AND s.ticker IN (SELECT ticker FROM {_SCOPE})"
    now = datetime.now(UTC).replace(tzinfo=None)
    frames: list[pd.DataFrame] = []
    lake.con.register(_SCOPE, pd.DataFrame({"ticker": list(tickers or [])}, dtype=object))
    try:
        for check in checks:
            df = lake.con.execute(check.sql.replace("{scope}", scope)).fetchdf()
            if df.empty:
                continue
            df = (
                df.groupby(["ticker", "period_end", "frequency"], as_index=False)["detail"]
                .agg(" | ".join)
                .assign(check_id=check.check_id, severity=check.severity, flagged_at=now)
            )
            frames.append(df[list(FLAG_COLUMNS)])
    finally:
        lake.con.unregister(_SCOPE)
    if not frames:
        return pd.DataFrame(columns=list(FLAG_COLUMNS))
    return pd.concat(frames, ignore_index=True)


def audit_statements(
    lake: DuckDBLake,
    tickers: Sequence[str] | None = None,
    *,
    checks: Sequence[StatementCheck] = CHECKS,
) -> AuditReport:
    """Run ``checks`` over ``tickers`` (every ticker when ``None``) and
    replace their rows in ``statement_flags``."""
    scope = None if tickers is None else list(dict.fromkeys(tickers))
    flags = find_flags(lake, scope, checks)
    lake.replace_statement_flags(flags, tickers=scope)
    counts = flags.groupby("check_id").size().to_dict() if not flags.empty else {}
    counts = {str(k): int(v) for k, v in counts.items()}
    _log.info("audit.statements.done", tickers=len(scope) if scope else "all", counts=counts)
    return AuditReport(tickers=scope, counts=counts, flags=flags)
