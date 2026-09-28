"""A portfolio's external cash flows (roadmap 20.5): deposits and
withdrawals recorded on a simulated book (``portfolio_cash_flows``) plus
the ones a broker sync brought in (``broker_activities``). Dividends,
interest and fees are not external flows: they stay inside the return.

Each flow carries its own currency and the currency of the account it
landed in. :func:`external_flows` converts a flow in another currency to
the account currency with :class:`stonks.fx.FxRates` and never guesses a
missing rate."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import date
from typing import Any

from stonks.fx import FxRateMissing, FxRates, normalize_currency
from stonks.insights.returns import Flow
from stonks.store.state import SqliteState


@dataclass(frozen=True)
class RecordedFlow:
    day: date
    kind: str  # deposit | withdrawal
    amount: float  # positive
    source: str  # manual | broker
    note: str | None = None
    id: int | None = None
    #: The currency the amount is in (the account currency when unknown).
    currency: str = "USD"
    #: The currency of the account the flow landed in.
    account_currency: str = "USD"

    @property
    def signed(self) -> float:
        return self.amount if self.kind == "deposit" else -self.amount


def _has_table(state: SqliteState, name: str) -> bool:
    return bool(state.sql("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", [name]))


def recorded_flows(state: SqliteState, portfolio_id: str) -> list[RecordedFlow]:
    """Every deposit and withdrawal of ``portfolio_id``, oldest first."""
    out: list[RecordedFlow] = []
    found = state.sql("SELECT base_currency FROM portfolios WHERE id = ?", [portfolio_id])
    base = _code(found[0]["base_currency"] if found else None, "USD")
    if _has_table(state, "portfolio_cash_flows"):
        for r in state.sql(
            "SELECT id, flow_date, kind, amount, note FROM portfolio_cash_flows"
            " WHERE portfolio_id = ? ORDER BY flow_date, id",
            [portfolio_id],
        ):
            out.append(
                RecordedFlow(
                    day=date.fromisoformat(r["flow_date"]),
                    kind=r["kind"],
                    amount=float(r["amount"]),
                    source="manual",
                    note=r["note"],
                    id=int(r["id"]),
                    currency=base,
                    account_currency=base,
                )
            )
    if _has_table(state, "broker_activities"):
        for r in state.sql(
            "SELECT a.trade_date, a.kind, a.amount, a.description, a.currency,"
            " acct.currency AS account_currency FROM broker_activities a"
            " LEFT JOIN broker_accounts acct ON acct.connection_id = a.connection_id"
            " AND acct.external_account_id = a.external_account_id"
            " WHERE a.portfolio_id = ? AND a.kind IN ('deposit', 'withdrawal')"
            " AND a.amount IS NOT NULL AND a.trade_date IS NOT NULL"
            " ORDER BY a.trade_date, a.id",
            [portfolio_id],
        ):
            amount = abs(float(r["amount"]))
            if amount <= 0:
                continue
            account = _code(r["account_currency"], base)
            out.append(
                RecordedFlow(
                    day=date.fromisoformat(str(r["trade_date"])[:10]),
                    kind=r["kind"],
                    amount=amount,
                    source="broker",
                    note=r["description"],
                    currency=_code(r["currency"], account),
                    account_currency=account,
                )
            )
    return sorted(out, key=lambda f: (f.day, f.source))


def _code(raw: object, default: str) -> str:
    text = str(raw).strip() if raw is not None else ""
    return text.upper() if text and text != "GBp" else (text or default)


FxLoader = Callable[[Iterable[str]], FxRates]


def _same(a: str, b: str) -> bool:
    return normalize_currency(a) == normalize_currency(b)


def external_flows(
    state: SqliteState, portfolio_id: str, *, fx_loader: FxLoader | None = None
) -> list[Flow]:
    """The flows as signed amounts in the account currency, for the return
    math. ``fx_loader`` is asked for rates (with the currencies involved)
    only when a flow is in another currency. A flow with no rate raises
    :class:`FxRateMissing`: a guessed flow would bend TWR and MWR."""
    recorded = recorded_flows(state, portfolio_id)
    foreign = [f for f in recorded if not _same(f.currency, f.account_currency)]
    fx: FxRates | None = None
    if foreign:
        if fx_loader is None:
            f = foreign[0]
            raise FxRateMissing(f.currency, f.account_currency, f.day)
        codes = {f.currency for f in foreign} | {f.account_currency for f in foreign}
        fx = fx_loader(sorted(codes))
    out: list[Flow] = []
    for f in recorded:
        if fx is None or _same(f.currency, f.account_currency):
            out.append(Flow(f.day, f.signed))
            continue
        out.append(
            Flow(f.day, fx.convert_or_raise(f.signed, f.currency, f.account_currency, f.day))
        )
    return out


def lake_fx_loader(open_lake: Callable[[], AbstractContextManager[Any]]) -> FxLoader:
    """An ``fx_loader`` that reads the lake's ``fx_rates`` on demand."""

    def load(codes: Iterable[str]) -> FxRates:
        from stonks.fx import load_fx_rates

        with open_lake() as lake:
            return load_fx_rates(lake, codes)

    return load


def flows_or_missing(
    state: SqliteState, portfolio_id: str, *, fx_loader: FxLoader | None = None
) -> tuple[list[Flow] | None, str | None]:
    """``(flows, None)``, or ``(None, currency)`` when a flow's currency has
    no rate. Callers then leave TWR and MWR out instead of guessing."""
    try:
        return external_flows(state, portfolio_id, fx_loader=fx_loader), None
    except FxRateMissing as exc:
        return None, exc.source
