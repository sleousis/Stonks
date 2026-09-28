"""Journeys for the Phase 20 console screens (roadmap 20.1 to 20.4):

- a trader places an order by hand from the ticket, and sees a refusal,
- a trader approves an order draft,
- a trader makes, switches off and deletes a price alert,
- the Telegram panel in Settings says the server has no bot.

Each test runs on desktop and on a 375px phone (the ``viewport`` fixture),
with a fresh trader and a paper portfolio of their own.
"""

from __future__ import annotations

import re
import uuid

import pyotp
import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import Visit
from tests.e2e.test_journeys import enrol

pytestmark = pytest.mark.e2e


def _trader(browse, stack, tag: str, viewport: str) -> Visit:
    """A new trader, signed in, with one paper portfolio."""
    person = stack.add_person("trader", f"{tag}-{viewport}@e2e.test", f"{tag.title()} Trader")
    person.totp_secret = pyotp.random_base32()
    enrol(stack, person)
    v: Visit = browse(person)
    made = v.api(
        "POST", "/api/portfolios", data={"name": f"{tag} {viewport}", "initial_cash": 100_000}
    )
    assert made.status == 201, made.text()
    return v


def test_a_trader_places_an_order_by_hand(browse, stack, viewport):
    v = _trader(browse, stack, "ticket", viewport)
    page = v.page
    v.go("/orders/new")
    expect(page.get_by_role("heading", name="New order")).to_be_visible()
    expect(page.get_by_role("link", name="New order")).to_have_attribute("aria-current", "page")

    page.get_by_label("Ticker").fill("aaa.us")
    page.get_by_label("Quantity").fill("3")
    page.get_by_label("Why this trade").fill("e2e manual buy")
    page.get_by_role("button", name="Check order").click()
    ticket = page.get_by_role("group", name="Checked order")
    expect(ticket).to_contain_text("AAA.US")
    expect(ticket).to_contain_text("PAPER")
    expect(ticket).to_contain_text("Every check passed")
    v.check_page("order-ticket")

    page.get_by_role("button", name="Place order").click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_contain_text("Buy 3 AAA.US?")
    dialog.get_by_role("button", name="Place order").click()
    expect(page.locator(".result")).to_contain_text("Filled")
    listed = page.locator("app-manual-orders-list")
    expect(listed).to_contain_text("AAA.US")

    # The ledger has it as a manual order.
    orders = v.api("GET", "/api/orders?origin=manual").json()["items"]
    assert any(o["ticker"] == "AAA.US" and o["origin"] == "manual" for o in orders)

    # Selling more than the book holds is refused on the ticket, never toasted.
    v.guard.expect_refusal(409, r"/api/orders/manual/preview$", "the order is refused")
    page.get_by_role("radio", name="Sell").click()
    page.get_by_label("Quantity").fill("5000")
    page.get_by_label("Why this trade").fill("too big")
    page.get_by_role("button", name="Check order").click()
    expect(page.locator("app-order-refusal, .failure").first).to_contain_text("Not placed")
    v.check_page("order-ticket-refused")
    v.guard.assert_clean()


def test_a_trader_approves_a_suggested_order(browse, stack, viewport):
    v = _trader(browse, stack, "drafts", viewport)
    page = v.page
    made = v.api(
        "POST",
        "/api/orders/drafts",
        data={
            "ticker": "AAA.US",
            "side": "buy",
            "quantity": 2,
            "reason": "e2e draft",
            "retry_key": f"e2e-{uuid.uuid4().hex[:12]}",
        },
    )
    assert made.status == 201, made.text()

    # F9: the old Drafts address lands on the one Approvals inbox.
    v.go("/orders/drafts")
    expect(page).to_have_url(re.compile(r"/tickets$"))
    expect(page.get_by_role("heading", level=1)).to_have_text("Approvals")
    card = page.locator("app-suggested-orders article.ticket", has_text="e2e draft")
    expect(card).to_contain_text("AAA.US")
    expect(card).to_contain_text("Suggested by")
    expect(card).to_contain_text("Waiting for you")
    v.check_page("approvals-suggested")

    card.get_by_role("button", name="Approve the suggested order for AAA.US").click()
    code_box = page.get_by_role("textbox", name="Code")
    dialog = page.get_by_role("dialog")
    # A fresh sign-in counts as a fresh code; ask again only when it is stale.
    if code_box.is_visible():
        code_box.fill(v.person.code())
        page.get_by_role("button", name="Confirm").click()
    expect(dialog).to_contain_text("Buy 2 AAA.US?")
    dialog.get_by_role("button", name="Approve and place").click()
    expect(page.get_by_text("Nothing waits for you")).to_be_visible()

    page.get_by_role("tab", name="History").click()
    placed = page.locator("app-suggested-orders article.ticket", has_text="e2e draft")
    expect(placed).to_contain_text("Placed")
    v.guard.assert_clean()


def test_a_trader_keeps_price_alerts(browse, stack, viewport):
    v = _trader(browse, stack, "alerts", viewport)
    page = v.page
    v.go("/notifications")
    page.get_by_role("link", name="Price alerts").click()
    expect(page.get_by_role("heading", level=1)).to_have_text("Price alerts")
    expect(page.get_by_text("No price alerts yet")).to_be_visible()
    v.check_page("price-alerts-empty")

    page.get_by_label("Ticker").fill("aaa.us")
    page.get_by_role("radio", name="Falls below").click()
    page.get_by_label("Price level").fill("5")
    page.get_by_label("Name").fill(f"Dip {viewport}")
    page.get_by_role("button", name="Save alert").click()
    row = page.locator(".alerts li", has_text=f"Dip {viewport}")
    expect(row).to_contain_text("Falls below 5")

    # A move alert on a watchlist.
    made = v.api(
        "POST", "/api/watchlists", data={"name": f"Swing {viewport}", "tickers": ["BBB.US"]}
    )
    assert made.status == 201, made.text()
    page.reload()
    page.get_by_role("radio", name="A watchlist").click()
    page.get_by_label("Watchlist", exact=True).select_option(label=f"Swing {viewport} (1)")
    page.get_by_role("radio", name="Moves by").click()
    page.get_by_label("Percent move").fill("8")
    page.get_by_label("Over how many days").fill("5")
    page.get_by_role("button", name="Save alert").click()
    swing = page.locator(".alerts li", has_text=f"Every ticker in Swing {viewport}")
    expect(swing).to_contain_text("Moves 8% either way in 5 days")
    v.check_page("price-alerts")

    # Off, then deleted.
    row.get_by_role("checkbox").uncheck()
    expect(row).to_have_class("off")
    row.get_by_role("button", name="Delete").click()
    page.get_by_role("dialog").get_by_role("button", name="Delete alert").click()
    expect(page.locator(".alerts li", has_text=f"Dip {viewport}")).to_have_count(0)
    rules = v.api("GET", "/api/price-alerts").json()["items"]
    assert [r["condition"] for r in rules] == ["moves_pct"]
    v.guard.assert_clean()


def test_the_telegram_panel_says_when_there_is_no_bot(browse, stack, viewport):
    v = _trader(browse, stack, "telegram", viewport)
    page = v.page
    v.go("/settings")
    panel = page.locator("app-telegram-link")
    expect(panel).to_contain_text("Telegram")
    expect(panel).to_contain_text("no Telegram bot yet")
    v.check_page("settings-telegram")
    v.guard.assert_clean()
