"""The trading run and approval tickets learn from the server how many books
trade real money (a portfolio at live_small or live_scale with an approve or
automatic follow), not only from the system broker. A paper system broker
with a live IBKR portfolio still moves real money."""

from __future__ import annotations

from stonks.accounts import PortfolioRepository, Role, Scope
from stonks.store.state import SqliteState


def _live_book(settings, user_id: str, *, stages: tuple[str, ...], mode: str = "auto") -> str:
    from stonks.production.live.stages import change_stage

    with SqliteState(settings.state.path) as state:
        pid = (
            PortfolioRepository(state)
            .create(Scope(user_id=user_id, role=Role.TRADER), name="Live", kind="broker")
            .id
        )
        state.execute(
            "INSERT INTO subscriptions (id, user_id, strategy_id, portfolio_id, mode,"
            " created_at, updated_at) VALUES (?, ?, 'bah_active', ?, ?, 'x', 'x')",
            [f"sub_{pid}", user_id, pid, mode],
        )
        for stage in stages:
            change_stage(state, pid, stage, actor="t", reason="setup",
                         gate_report={"target": stage, "passed": True})  # fmt: skip
    return pid


def test_the_broker_info_counts_books_that_trade_real_money(client, settings, people):
    headers = people["ada"]["headers"]
    info = client.get("/api/brokers", headers=headers).json()
    assert info["kind"] == "simulated" and info["real_money_books"] == 0
    _live_book(settings, people["alice"]["id"], stages=("broker_paper",))
    assert client.get("/api/brokers", headers=headers).json()["real_money_books"] == 0
    _live_book(settings, people["bob"]["id"], stages=("broker_paper", "live_small"))
    assert client.get("/api/brokers", headers=headers).json()["real_money_books"] == 1


def test_the_approval_preview_counts_followers_at_real_money(client, settings, people):
    headers = people["ada"]["headers"]
    url = "/api/strategies/bah_active/golive"
    assert client.get(url, headers=headers).json()["real_money_books"] == 0
    _live_book(settings, people["bob"]["id"], stages=("broker_paper", "live_small"),
               mode="approve")  # fmt: skip
    assert client.get(url, headers=headers).json()["real_money_books"] == 1
