"""Live settings journeys (roadmap 19.4, 19.6, 19.7): a trader with a
real-money portfolio sets its allocation and account profile, sees that
there are no automatic ramp steps and which live rules act on it, and reads
an order whose outcome is unknown. An admin reads the broker gateway health
on the Health page. Runs on desktop and on a 375px phone.

Each run uses its own trader and live portfolio, so the shared trader's
figures stay as the other journeys expect them.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pyotp
import pytest
from playwright.sync_api import expect

from stonks.accounts import PortfolioRepository, Scope
from stonks.accounts.users import UserRepository
from stonks.store.state import SqliteState
from tests.e2e.stack import Person, Stack

pytestmark = pytest.mark.e2e


def _live_trader(stack: Stack, viewport: str) -> tuple[Person, str]:
    """A trader with a second factor and one broker portfolio (real money)
    holding one order the broker never confirmed."""
    from stonks.security.crypto import SecretBox

    person = stack.add_person("trader", f"live-{viewport}@e2e.test", "Lou Live")
    person.totp_secret = pyotp.random_base32()
    box = SecretBox.from_env(stack.env)
    sealed = box.seal(person.totp_secret.encode(), aad=f"users.totp_secret:{person.user_id}")
    now = datetime.now(UTC).isoformat()
    with SqliteState(stack.data_dir / "state.sqlite") as state:
        state.execute(
            "UPDATE users SET totp_secret_enc = ?, mfa_enrolled_at = '2026-01-01' WHERE id = ?",
            [sealed.token, person.user_id],
        )
        user = UserRepository(state).get(person.user_id)
        name = f"Lou live {viewport}"
        pid = PortfolioRepository(state).create(Scope.for_user(user), name=name, kind="broker").id
        state.execute(
            "INSERT INTO orders (client_id, ticker, side, quantity, order_type, status, state,"
            " created_at, updated_at, portfolio_id)"
            " VALUES (?, 'AAA.US', 'buy', 5, 'market', 'pending', 'unknown', ?, ?, ?)",
            [f"e2e-live-unknown-{viewport}", now, now, pid],
        )
    return person, name


def _seed_gateway(stack: Stack) -> None:
    """One live gateway the broker health check found down."""
    with SqliteState(stack.data_dir / "state.sqlite") as state:
        state.execute(
            "INSERT OR REPLACE INTO broker_gateway_status (gateway, mode, connected,"
            " last_check_at, last_ok_at, down_since, consecutive_failures, fault, detail)"
            " VALUES ('ibkr-live', 'live', 0, '2026-09-27T14:00:00+00:00',"
            " '2026-09-25T14:00:00+00:00', '2026-09-25T14:05:00+00:00', 12,"
            " 'login_refused', 'login refused by the broker')"
        )


def test_a_trader_sets_live_allocation_and_profile(browse, stack, viewport):
    person, name = _live_trader(stack, viewport)
    v = browse(person)
    page = v.page
    # The API answers 404 until the owner saves a profile; the page reads it as "Not set".
    v.guard.expect_refusal(404, r"/live/account-profile$", "no account profile saved yet")

    v.go("/profile")
    page.get_by_role("link", name=f"Real-money settings of {name}").click()
    expect(page.get_by_role("heading", level=1)).to_have_text("Real-money settings")
    figure = page.get_by_test_id("allocation-figure")
    expect(figure).to_have_text("Not set")
    expect(page.get_by_text("No automatic steps.")).to_be_visible()
    v.check_page("live settings")

    page.get_by_label("Amount").fill("2500")
    page.get_by_label("Reason").fill("first small slice")
    page.get_by_role("button", name="Set allocation").click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_contain_text("2,500")
    dialog.get_by_role("button", name="Set allocation").click()
    expect(figure).to_contain_text("2,500")
    expect(page.get_by_text("Why: first small slice")).to_be_visible()

    page.get_by_role("radio", name="UK").click()
    expect(page.get_by_text("two trading days later")).to_be_visible()
    page.get_by_role("button", name="Save profile").click()
    page.get_by_role("dialog").get_by_role("button", name="Save profile").click()
    # The confirm sheet shows the same words, so wait for the saved line and
    # the toast: checking earlier races the save and its rising toast.
    expect(page.get_by_text("Saved: UK, cash account, retail client")).to_be_visible()
    expect(page.get_by_role("status").filter(has_text="Saved the account profile")).to_be_visible()

    safeguards = page.locator("section[aria-labelledby='safeguards-title']")
    expect(safeguards).to_contain_text("Allocation cap")
    expect(safeguards).to_contain_text("Off")
    v.check_page("live settings saved")

    # F52: the checklist walks the same path, and shows these two steps done.
    page.get_by_role("link", name="Going live checklist").click()
    expect(page.get_by_role("heading", level=1)).to_have_text("Going live")
    steps = page.locator("ol.steps")
    expect(steps.locator("li[data-step='allocation'] .state")).to_have_text("Done")
    expect(steps.locator("li[data-step='profile'] .state")).to_have_text("Done")
    expect(steps.locator("li[data-step='stage'] .state")).to_have_text("Not yet")
    v.check_page("going live checklist")

    v.go("/orders")
    expect(page.get_by_text("Outcome unknown").first).to_be_visible()
    expect(page.get_by_text("Nothing is sent again").first).to_be_visible()
    v.check_page("orders with an unknown outcome")


def test_an_admin_reads_broker_gateway_health(browse, stack, viewport):
    _seed_gateway(stack)
    v = browse(stack.admin)
    page = v.page
    v.go("/health")
    panel = page.locator("section[aria-labelledby='gateways-title']")
    expect(panel).to_contain_text("ibkr-live")
    expect(panel).to_contain_text("Down")
    expect(panel).to_contain_text("The broker refused the login")
    expect(panel).to_contain_text("Last good check")
    v.check_page("health with a gateway down")
