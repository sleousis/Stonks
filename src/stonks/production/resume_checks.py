"""The checks shown before a kill switch is resumed (roadmap 23.15).

A person who types RESUME TRADING first sees, per portfolio the switch
covers that trades at a real broker:

- ``gateway_up``: the broker answers a positions read now;
- ``last_reconcile_clean``: the newest reconcile report of the portfolio
  is ``clean`` or ``warn`` (``None``, shown and not blocking, when there
  is none yet);
- ``account_readable``: the account's balances read now;
- ``equity_cover``: net liquidation is at least ``min_equity_multiple``
  times the largest position's value (``None`` when a position has no
  price).

A check whose answer is unknown (``passed is None``) is shown and never
blocks. The checks only read: nothing is sent and nothing is written. A
simulated book has no broker, so it gets one ``broker`` row with nothing
to check (P40: resuming stays a logged human action either way).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from stonks.logging import get_logger
from stonks.production.live.settings import ResumeCheckSettings
from stonks.store.state import SqliteState

_log = get_logger("stonks.production.resume_checks")

#: Reconcile statuses a resume accepts (``warn``: only items that alert).
_CLEAN = ("clean", "warn")


@dataclass(frozen=True)
class ResumeCheck:
    name: str
    #: ``None``: unknown, shown and not blocking.
    passed: bool | None
    detail: str
    portfolio_id: str | None = None


def checks_passed(checks: list[ResumeCheck]) -> bool:
    return all(c.passed is not False for c in checks)


def portfolio_resume_checks(
    state: SqliteState,
    portfolio_id: str,
    broker: Any | None,
    *,
    settings: ResumeCheckSettings,
    prices: Mapping[str, float] | None = None,
) -> list[ResumeCheck]:
    """The checks for one portfolio. ``broker`` is ``None`` for a book that
    trades at no external broker. ``prices`` marks positions the broker's
    own quotes do not price."""
    if broker is None:
        return [ResumeCheck("broker", None, "trades at no external broker", portfolio_id)]
    checks: list[ResumeCheck] = []
    positions: dict[str, float] | None = None
    try:
        positions = {t: q for t, q in broker.fetch_portfolio().positions.items() if abs(q) > 1e-12}
        checks.append(ResumeCheck("gateway_up", True, "the broker answered", portfolio_id))
    except Exception as exc:
        checks.append(
            ResumeCheck("gateway_up", False, f"the broker did not answer: {exc}", portfolio_id)
        )
    checks.append(_last_reconcile(state, portfolio_id))
    equity: float | None = None
    fetch_account = getattr(broker, "fetch_account", None)
    if fetch_account is None:
        checks.append(
            ResumeCheck("account_readable", None, "the broker has no account read", portfolio_id)
        )
    else:
        try:
            equity = float(fetch_account().equity)
            checks.append(
                ResumeCheck(
                    "account_readable", True, f"net liquidation {equity:,.2f}", portfolio_id
                )
            )
        except Exception as exc:
            checks.append(
                ResumeCheck(
                    "account_readable", False, f"the account did not read: {exc}", portfolio_id
                )
            )
    checks.append(_equity_cover(broker, portfolio_id, positions, equity, settings, prices or {}))
    return checks


def _last_reconcile(state: SqliteState, portfolio_id: str) -> ResumeCheck:
    name = "last_reconcile_clean"
    tables = state.sql(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'reconcile_reports'"
    )
    if not tables:
        return ResumeCheck(name, None, "no reconcile reports table", portfolio_id)
    rows = state.sql(
        "SELECT kind, as_of, status FROM reconcile_reports WHERE portfolio_id = ?"
        " ORDER BY taken_at DESC LIMIT 1",
        [portfolio_id],
    )
    if not rows:
        return ResumeCheck(name, None, "no reconcile report yet", portfolio_id)
    r = rows[0]
    detail = f"{r['kind']} check of {r['as_of']}: {r['status']}"
    return ResumeCheck(name, r["status"] in _CLEAN, detail, portfolio_id)


def _equity_cover(
    broker: Any,
    portfolio_id: str,
    positions: dict[str, float] | None,
    equity: float | None,
    settings: ResumeCheckSettings,
    prices: Mapping[str, float],
) -> ResumeCheck:
    name = "equity_cover"
    need = settings.min_equity_multiple
    if positions is None or equity is None:
        return ResumeCheck(name, None, "unknown: positions or equity did not read", portfolio_id)
    if not positions:
        return ResumeCheck(name, True, "no positions", portfolio_id)
    marks = dict(prices)
    missing = [t for t in positions if not marks.get(t)]
    if missing and hasattr(broker, "quotes"):
        try:
            for ticker, quote in broker.quotes(missing).items():
                ref = quote.reference
                if ref is not None and ref > 0:
                    marks[ticker] = float(ref)
        except Exception as exc:
            _log.warning("resume_checks.quotes_failed", portfolio_id=portfolio_id, error=str(exc))
    unpriced = sorted(t for t in positions if not marks.get(t))
    if unpriced:
        return ResumeCheck(name, None, f"no price for {', '.join(unpriced[:5])}", portfolio_id)
    ticker, value = max(
        ((t, abs(q) * marks[t]) for t, q in positions.items()), key=lambda tv: tv[1]
    )
    multiple = equity / value if value > 0 else float("inf")
    detail = (
        f"net liquidation {equity:,.2f} is {multiple:.1f}x the largest position"
        f" ({ticker}, {value:,.2f}), need {need:g}x"
    )
    return ResumeCheck(name, multiple >= need, detail, portfolio_id)
