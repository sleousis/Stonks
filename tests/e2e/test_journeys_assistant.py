"""Phase 20 console journeys: the AI assistant, cash flows and tax.

- A trader asks the assistant about their portfolio (a read step and a
  streamed answer), asks it to stop trading (the write waits for their yes)
  and rejects it, reads the trace, then starts a research-only conversation
  where the same ask changes nothing. The stack serves the assistant from
  :mod:`tests.e2e.fake_assistant`, never a model server.
- A trader records a deposit on their paper book as a ticket, then switches
  their tax lot method and downloads the realized gains file.

Each run uses its own trader, so the shared trader's figures stay as the
other journeys expect them. Runs on desktop and on a 375px phone.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta

import pyotp
import pytest
from playwright.sync_api import expect

from stonks.accounts import PortfolioRepository, Scope
from stonks.accounts.users import UserRepository
from stonks.store.state import SqliteState
from tests.e2e.conftest import Visit
from tests.e2e.fake_assistant import DECLINED_ANSWER, READ_ANSWER, REFUSED_ANSWER
from tests.e2e.stack import Person, Stack

pytestmark = pytest.mark.e2e

DEPOSIT = re.compile(r"\+\S*500\.00")


def _trader(stack: Stack, viewport: str, tag: str) -> Person:
    """A trader with a second factor and one paper book with a cash snapshot."""
    from stonks.security.crypto import SecretBox

    person = stack.add_person("trader", f"{tag}-{viewport}@e2e.test", "Pat Phase")
    person.totp_secret = pyotp.random_base32()
    box = SecretBox.from_env(stack.env)
    sealed = box.seal(person.totp_secret.encode(), aad=f"users.totp_secret:{person.user_id}")
    day = stack.market_end - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    with SqliteState(stack.data_dir / "state.sqlite") as state:
        state.execute(
            "UPDATE users SET totp_secret_enc = ?, mfa_enrolled_at = '2026-01-01' WHERE id = ?",
            [sealed.token, person.user_id],
        )
        user = UserRepository(state).get(person.user_id)
        pid = PortfolioRepository(state).create(Scope.for_user(user), name=f"{tag} book").id
        tick_id = f"tick-e2e-{tag}-{viewport}"
        state.execute(
            "INSERT INTO tick_runs (id, started_at, finished_at, status) VALUES (?, ?, ?, 'ok')",
            [tick_id, f"{day}T21:00:00+00:00", f"{day}T21:01:00+00:00"],
        )
        state.execute(
            "INSERT INTO portfolio_snapshots (tick_id, portfolio_id, as_of, taken_at, cash,"
            " positions_json, total_value) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                tick_id,
                pid,
                day.isoformat(),
                f"{day}T21:00:00+00:00",
                1000.0,
                json.dumps({}),
                1000.0,
            ],
        )
    return person


def _send(v: Visit, text: str) -> None:
    v.page.get_by_label("Message", exact=True).fill(text)
    v.page.get_by_role("button", name="Send", exact=True).click()


def test_a_trader_asks_the_assistant_and_approves_nothing_by_accident(browse, stack, viewport):
    v = browse(_trader(stack, viewport, "assistant"))
    page = v.page
    v.open_nav()
    page.get_by_role("link", name="Assistant", exact=True).first.click()
    expect(page.get_by_role("heading", level=1)).to_have_text("Assistant")
    expect(page.get_by_text("The assistant is off")).to_have_count(0)

    # A read runs at once, shown as a step, and the answer streams in.
    _send(v, "How is my portfolio doing?")
    transcript = page.locator(".transcript")
    expect(transcript).to_contain_text("Read your portfolio")
    expect(transcript).to_contain_text(READ_ANSWER)
    expect(page).to_have_url(re.compile(r"/assistant\?c="))
    expect(page.locator("#chat-title")).to_have_text("How is my portfolio doing?")

    # A write waits for a yes: nothing runs, and saying no keeps it that way.
    _send(v, "Please stop trading")
    confirm = page.locator("app-confirm-step")
    expect(confirm).to_contain_text("Stop trading")
    expect(confirm).to_contain_text("Nothing runs until you approve it.")
    v.check_page("assistant-confirm")
    confirm.get_by_role("button", name="Reject").click()
    expect(confirm).to_contain_text("Rejected")
    expect(transcript).to_contain_text(DECLINED_ANSWER)
    halts = v.api("GET", "/api/halts").json()
    assert not [h for h in halts["items"] if h.get("user_id") == v.person.user_id]

    # The trace names the model and every tool call of each turn.
    page.get_by_role("button", name="Trace", exact=True).click()
    trace = page.get_by_role("dialog")
    expect(trace).to_contain_text("How the assistant got there")
    expect(trace).to_contain_text("Model e2e-keyword")
    expect(trace).to_contain_text("Asked for your yes")
    v.check_page("assistant-trace")
    trace.get_by_role("button", name="Close").click()

    # Research only: the same ask cannot change anything.
    page.get_by_role("button", name="New conversation").click()
    expect(page.locator("#chat-title")).to_have_text("New conversation")
    page.get_by_label("Research only").check()
    _send(v, "stop trading now")
    expect(page.locator(".transcript")).to_contain_text(REFUSED_ANSWER)
    expect(page.locator("app-confirm-step")).to_have_count(0)
    expect(page.locator(".chat-head .tag")).to_have_text("Research only")
    v.check_page("assistant")


def test_a_trader_records_a_deposit_and_sets_up_tax(browse, stack, viewport):
    v = browse(_trader(stack, viewport, "money"))
    page = v.go("/insights/cash-flows")
    expect(page.get_by_role("heading", level=1)).to_have_text("Cash flows")
    flows = page.locator(
        "section", has=page.get_by_role("heading", name="Deposits and withdrawals")
    )
    expect(flows).to_contain_text("No deposits or withdrawals yet")

    page.get_by_label("Amount (USD)").fill("500")
    page.get_by_label("Note (optional)").fill("Monthly saving")
    page.locator("form").get_by_role("button", name="Record deposit").click()
    ticket = page.get_by_role("dialog")
    expect(ticket).to_contain_text("Deposit")
    expect(ticket).to_contain_text("PAPER")
    ticket.get_by_role("button", name="Record deposit").click()
    # The money format follows the browser's locale ("+$500.00", "+US$500.00").
    expect(flows).to_contain_text(DEPOSIT)
    expect(flows).to_contain_text("Recorded here")
    expect(page.locator(".tiles")).to_contain_text(DEPOSIT)
    v.check_page("cash-flows")

    page.get_by_role("link", name="Tax", exact=True).click()
    expect(page.get_by_role("heading", level=1)).to_have_text("Tax")
    expect(page.get_by_label("Base currency")).to_have_value("USD")
    page.get_by_role("radio", name="Specific lots").click()
    page.get_by_role("button", name="Save settings").click()
    expect(page.get_by_text("No sales yet")).to_be_visible()

    with page.expect_download() as download:
        page.get_by_role("button", name="Realized gains CSV").click()
    assert re.fullmatch(r"stonks-tax-gains-\d{4}\.csv", download.value.suggested_filename)
    # The open lots on a day come as a CSV too (13.12).
    page.get_by_label("Open lots on").fill("2026-06-30")
    with page.expect_download() as lots:
        page.get_by_role("button", name="Open lots CSV").click()
    assert lots.value.suggested_filename == "stonks-tax-lots-2026-06-30.csv"
    v.check_page("tax")
