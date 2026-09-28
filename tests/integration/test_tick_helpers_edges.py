"""Tick helpers at their edges: who owns a holding when its attribution is
gone, and the broker's what-if preview written on each ticket."""

from __future__ import annotations

from stonks.core.types import Order
from stonks.execution.brokers.base import MarginPreview
from stonks.logging import get_logger
from stonks.production.tick import _holding_owners, _what_if

PF = "pf_default"


def _filled(state, cid, ticker, sid, updated, status="filled"):
    state.execute(
        "INSERT INTO strategies (id, class_path, params_json, artifact_path, status, created_at,"
        " updated_at) VALUES (?, 'x.Y', '{}', 'a', 'active', 'x', 'x') ON CONFLICT DO NOTHING",
        [sid],
    )
    state.execute(
        "INSERT INTO orders (client_id, strategy_id, ticker, side, quantity, order_type, status,"
        " created_at, updated_at, portfolio_id) VALUES (?, ?, ?, 'buy', 1, 'market', ?, ?, ?, ?)",
        [cid, sid, ticker, status, updated, updated, PF],
    )


def test_a_holding_belongs_to_its_largest_attribution_else_its_latest_fill(state):
    _filled(state, "o1", "B.US", "s_old", "2026-03-01T20:00:00+00:00")
    _filled(state, "o2", "B.US", "s_new", "2026-03-05T20:00:00+00:00")
    _filled(state, "o3", "B.US", "s_rejected", "2026-03-06T20:00:00+00:00", status="rejected")
    prior = {"A.US": {"s_small": 0.2, "s_big": -0.8}, "C.US": {}}
    owners = _holding_owners(state, PF, ["A.US", "B.US", "C.US"], prior)
    # C.US has neither an attribution nor a fill of a strategy: nobody owns it
    assert owners == {"A.US": "s_big", "B.US": "s_new"}


class _Previewer:
    def __init__(self) -> None:
        self.asked: list[str] = []

    def what_if(self, order: Order) -> MarginPreview:
        self.asked.append(order.client_id)
        if order.ticker == "BAD.US":
            raise TimeoutError("no answer from the gateway " + "x" * 300)
        return MarginPreview(client_id=order.client_id, initial_margin_change=100.0,
                             maintenance_margin_change=80.0, equity_with_loan_after=9_900.0,
                             commission=1.0, commission_currency="USD",
                             warning="close to the limit")  # fmt: skip


def test_the_what_if_preview_is_noted_per_order_and_a_failure_never_stops_it():
    orders = [
        Order(client_id="ok", ticker="A.US", side="buy", quantity=1.0),
        Order(client_id="bad", ticker="BAD.US", side="buy", quantity=1.0),
    ]
    broker = _Previewer()
    out = _what_if(broker, orders, get_logger("test"))  # type: ignore[arg-type]
    assert broker.asked == ["ok", "bad"]
    assert out["ok"]["what_if"] == {
        "commission": 1.0,
        "commission_currency": "USD",
        "initial_margin_change": 100.0,
        "maintenance_margin_change": 80.0,
        "equity_with_loan_after": 9_900.0,
        "warning": "close to the limit",
    }
    assert out["bad"]["what_if_error"].startswith("no answer from the gateway")
    assert len(out["bad"]["what_if_error"]) == 200
    assert _what_if(None, orders, get_logger("test")) == {}
