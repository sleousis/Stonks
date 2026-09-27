"""Order tickets over the API (roadmap 19.8): read your own, approve with a
fresh second factor (tokens never can), reject with a reason, and an
operator sends the due ones."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from stonks.accounts import PortfolioRepository, Scope
from stonks.core.types import Order
from stonks.production.tickets import SubmitWindow, write_tickets
from stonks.store.state import SqliteState
from tests.integration.app.stepup import allow_step_up


def _book_with_tickets(settings, person: dict, *, n: int = 2, hold: str | None = "approve_mode"):
    now = datetime.now(UTC)
    with SqliteState(settings.state.path) as state:
        scope = Scope(user_id=person["id"], role=person["role"])
        pf = PortfolioRepository(state).create(scope, name="Growth", kind="broker").id
        orders = [
            Order(
                client_id=f"2026-03-17:{pf}:s1:T{i}.US:buy",
                ticker=f"T{i}.US",
                side="buy",
                quantity=5.0,
                strategy_id="s1",
                portfolio_id=pf,
                decision_price=20.0,
                decision_context={"score": 0.1 * (i + 1)},
            )
            for i in range(n)
        ]
        tickets = write_tickets(
            state,
            orders,
            portfolio_id=pf,
            tick_id=None,
            as_of=date(2026, 3, 17),
            window=SubmitWindow(now - timedelta(minutes=5), now + timedelta(hours=1)),
            hold=lambda _: hold,  # type: ignore[arg-type,return-value]
            now=now,
        )
    return pf, [t.id for t in tickets]


def test_you_see_only_your_own_tickets(client, settings, people):
    pf, ids = _book_with_tickets(settings, people["alice"])
    mine = client.get("/api/tickets", headers=people["alice"]["headers"])
    assert mine.status_code == 200, mine.text
    body = mine.json()
    assert body["total"] == 2 and {t["id"] for t in body["items"]} == set(ids)
    first = body["items"][0]
    assert first["portfolio_name"] == "Growth" and first["hold"] == "approve_mode"
    assert first["notional"] == 100.0 and first["reference_price"] == 20.0
    assert first["status"] == "awaiting_approval"

    bob = client.get("/api/tickets", headers=people["bob"]["headers"]).json()
    assert bob["total"] == 0
    assert client.get(f"/api/tickets/{ids[0]}", headers=people["bob"]["headers"]).status_code == 404
    hidden = client.get(f"/api/tickets?portfolio_id={pf}", headers=people["bob"]["headers"])
    assert hidden.status_code == 404

    summary = client.get("/api/tickets/summary", headers=people["alice"]["headers"]).json()
    assert summary == {"awaiting_approval": 2, "by_portfolio": {pf: 2}}
    one = client.get(f"/api/tickets/{ids[0]}", headers=people["alice"]["headers"])
    assert one.status_code == 200 and one.json()["id"] == ids[0]


def test_approving_needs_a_fresh_second_factor(app, client, settings, people):
    _, ids = _book_with_tickets(settings, people["alice"])
    alice = people["alice"]["headers"]
    token = client.post("/api/tickets/approve", json={"ticket_ids": ids}, headers=alice)
    assert token.status_code == 403 and token.json()["code"] == "step_up_required"

    allow_step_up(app)
    ok = client.post("/api/tickets/approve", json={"ticket_ids": ids}, headers=alice)
    assert ok.status_code == 200, ok.text
    assert {t["status"] for t in ok.json()["items"]} == {"approved"}
    again = client.post("/api/tickets/approve", json={"ticket_ids": ids[:1]}, headers=alice)
    assert again.status_code == 409  # decided already
    with SqliteState(settings.state.path) as state:
        actions = [r["action"] for r in state.sql("SELECT action FROM audit_log")]
    assert actions.count("ticket.approve") == 2


def test_nobody_approves_another_persons_ticket(app, client, settings, people):
    _, ids = _book_with_tickets(settings, people["alice"])
    allow_step_up(app)
    bob = client.post(
        "/api/tickets/approve", json={"ticket_ids": ids}, headers=people["bob"]["headers"]
    )
    assert bob.status_code == 404
    viewer = client.post(
        "/api/tickets/approve", json={"ticket_ids": ids}, headers=people["vic"]["headers"]
    )
    assert viewer.status_code == 403


def test_reject_needs_a_reason(client, settings, people):
    _, ids = _book_with_tickets(settings, people["alice"])
    alice = people["alice"]["headers"]
    empty = client.post(f"/api/tickets/{ids[0]}/reject", json={"reason": ""}, headers=alice)
    assert empty.status_code == 422
    done = client.post(
        f"/api/tickets/{ids[0]}/reject", json={"reason": "too big today"}, headers=alice
    )
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "rejected"
    assert done.json()["decision_reason"] == "too big today"


def test_a_bad_ticket_id_is_refused(app, client, people):
    allow_step_up(app)
    bad = client.post(
        "/api/tickets/approve",
        json={"ticket_ids": ["drop table"]},
        headers=people["alice"]["headers"],
    )
    assert bad.status_code == 422


def test_submit_is_an_operator_action(client, settings, people):
    _book_with_tickets(settings, people["alice"], n=1, hold=None)
    trader = client.post("/api/tickets/submit", headers=people["alice"]["headers"])
    assert trader.status_code == 403
    admin = client.post("/api/tickets/submit", headers=people["ada"]["headers"])
    assert admin.status_code == 200, admin.text
    [pf] = admin.json()["portfolios"]
    # the broker portfolio has no connection linked: reported, the ticket kept
    assert pf["status"] == "error" and admin.json()["sent"] == 0
