"""A portfolio's external cash flows (roadmap 20.5): deposits and
withdrawals recorded on a simulated book (``portfolio_cash_flows``) plus
the ones a broker sync brought in (``broker_activities``). Dividends,
interest and fees are not external flows: they stay inside the return."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

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

    @property
    def signed(self) -> float:
        return self.amount if self.kind == "deposit" else -self.amount


def _has_table(state: SqliteState, name: str) -> bool:
    return bool(state.sql("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", [name]))


def recorded_flows(state: SqliteState, portfolio_id: str) -> list[RecordedFlow]:
    """Every deposit and withdrawal of ``portfolio_id``, oldest first."""
    out: list[RecordedFlow] = []
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
                )
            )
    if _has_table(state, "broker_activities"):
        for r in state.sql(
            "SELECT trade_date, kind, amount, description FROM broker_activities"
            " WHERE portfolio_id = ? AND kind IN ('deposit', 'withdrawal')"
            " AND amount IS NOT NULL AND trade_date IS NOT NULL ORDER BY trade_date, id",
            [portfolio_id],
        ):
            amount = abs(float(r["amount"]))
            if amount <= 0:
                continue
            out.append(
                RecordedFlow(
                    day=date.fromisoformat(str(r["trade_date"])[:10]),
                    kind=r["kind"],
                    amount=amount,
                    source="broker",
                    note=r["description"],
                )
            )
    return sorted(out, key=lambda f: (f.day, f.source))


def external_flows(state: SqliteState, portfolio_id: str) -> list[Flow]:
    """The flows as signed amounts for the return math."""
    return [Flow(f.day, f.signed) for f in recorded_flows(state, portfolio_id)]
