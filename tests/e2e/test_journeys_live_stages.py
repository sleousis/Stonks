"""Live stage journeys (roadmap 19.9): a trader with a broker portfolio
whose strategy finished its paper days reads the stage card, moves up to
broker paper after the typed stage name, asks for an order preview (the
portfolio has no live book yet, so it is told so and nothing is sent), then
moves back down with a reason. Runs on desktop and on a 375px phone.

Each run uses its own trader and portfolio, so the other journeys' figures
stay as they expect them.
"""

from __future__ import annotations

import pyotp
import pytest
from playwright.sync_api import expect

from stonks.accounts import PortfolioRepository, Scope
from stonks.accounts.users import UserRepository
from stonks.store.state import SqliteState
from tests.e2e.stack import ACTIVE_IDS, Person, Stack

pytestmark = pytest.mark.e2e


def _staged_trader(stack: Stack, viewport: str) -> tuple[Person, str]:
    """A trader with a second factor, one broker portfolio and one paper
    subscription that finished its 20 paper days."""
    from stonks.security.crypto import SecretBox

    person = stack.add_person("trader", f"stage-{viewport}@e2e.test", "Sam Stage")
    person.totp_secret = pyotp.random_base32()
    box = SecretBox.from_env(stack.env)
    sealed = box.seal(person.totp_secret.encode(), aad=f"users.totp_secret:{person.user_id}")
    strategy = next(iter(ACTIVE_IDS.values()))
    with SqliteState(stack.data_dir / "state.sqlite") as state:
        state.execute(
            "UPDATE users SET totp_secret_enc = ?, mfa_enrolled_at = '2026-01-01' WHERE id = ?",
            [sealed.token, person.user_id],
        )
        user = UserRepository(state).get(person.user_id)
        name = f"Sam stage {viewport}"
        pid = PortfolioRepository(state).create(Scope.for_user(user), name=name, kind="broker").id
        state.execute(
            "INSERT INTO subscriptions (id, user_id, strategy_id, portfolio_id, mode,"
            " paper_days_completed, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, 'paper', 20, '2026-08-01', '2026-08-01')",
            [f"sub_stage_{viewport}", person.user_id, strategy, pid],
        )
    return person, pid


def test_a_trader_moves_a_portfolio_up_and_down_the_stages(browse, stack, viewport):
    person, pid = _staged_trader(stack, viewport)
    v = browse(person)
    page = v.page
    v.guard.expect_refusal(404, r"/live/account-profile$", "no account profile saved yet")
    v.guard.expect_refusal(422, r"/live/preview$", "no live book to preview yet")

    v.go(f"/profile/live/{pid}")
    card = page.locator("app-live-stage-card")
    expect(card.locator(".step[aria-current='step']")).to_contain_text("Simulated")
    expect(card).to_contain_text("To move up to Broker paper")
    expect(card.locator(".checks")).to_contain_text("Paper days done")
    expect(card).to_contain_text("never moves the stage or the amount")
    v.check_page("live stage card")

    card.get_by_label("Why now").fill("the paper days are done")
    card.get_by_role("button", name="Move up to Broker paper").click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_contain_text("Stage change")
    dialog.locator("#confirm-typed").fill("Broker paper")
    dialog.get_by_role("button", name="Move to Broker paper").click()
    expect(card.locator(".step[aria-current='step']")).to_contain_text("Broker paper")
    expect(card).to_contain_text("To move up to Real money, small")
    expect(card.get_by_text("Stage changes (1)")).to_be_visible()

    preview = page.locator("app-live-preview-panel")
    preview.get_by_role("button", name="Preview the next orders").click()
    expect(preview).to_contain_text("Could not preview the orders")
    v.check_page("live stage moved up")

    card.get_by_text("Move down a stage").click()
    card.get_by_label("Why move down").fill("not ready for the broker yet")
    card.get_by_role("button", name="Move down", exact=True).click()
    page.get_by_role("dialog").get_by_role("button", name="Move down to Simulated").click()
    expect(card.locator(".step[aria-current='step']")).to_contain_text("Simulated")
    expect(card.get_by_text("Stage changes (2)")).to_be_visible()
