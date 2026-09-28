"""Journeys for the trader workspace (roadmap 13.2, 13.4, 13.5, 13.12):

- a new trader walks the first-run guide from Today to "You are set up",
- an admin sees whether the install is ready,
- a trader opens a price chart from a watchlist and downloads a CSV.

Each test runs on desktop and on a 375px phone (the ``viewport`` fixture).
"""

from __future__ import annotations

import re

import pyotp
import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import Visit
from tests.e2e.stack import ACTIVE_IDS
from tests.e2e.test_journeys import enrol

pytestmark = pytest.mark.e2e


def test_a_new_trader_walks_the_first_run_guide(browse, stack, viewport):
    person = stack.add_person("trader", f"guide-{viewport}@e2e.test", "Guide Trader")
    person.totp_secret = pyotp.random_base32()
    enrol(stack, person)
    v: Visit = browse(person)
    page = v.page

    # Today offers the guide until it is done.
    card = page.locator("app-setup-card")
    expect(card).to_contain_text("Finish setting up")
    expect(card).to_contain_text("1 of 5 done")
    card.get_by_role("link", name="Continue setup").click()
    expect(page.get_by_role("heading", level=1)).to_have_text("Get set up")
    step = page.locator(".step.open")
    expect(step).to_contain_text("Pick a portfolio")
    v.check_page("welcome")

    # A paper portfolio.
    step.get_by_label("New paper portfolio").fill(f"Guide {viewport}")
    step.get_by_role("button", name="Open paper portfolio").click()
    expect(page.locator(".step.open")).to_contain_text("Choose what to watch")

    # A watchlist.
    step = page.locator(".step.open")
    step.get_by_label("Tickers").fill("aaa.us bbb.us")
    step.get_by_role("button", name="Save watchlist").click()
    expect(page.locator(".step.open")).to_contain_text("Follow a strategy")

    # Follow a live strategy for signals.
    step = page.locator(".step.open")
    sid = ACTIVE_IDS["AAA.US"]
    step.get_by_label("Strategy").select_option(sid)
    step.get_by_label("Signals only").check()
    step.get_by_role("button", name="Follow", exact=True).click()
    expect(page.locator(".step.open")).to_contain_text("Turn on alerts")

    # Skip alerts: every step is handled.
    page.locator(".step.open").get_by_role("button", name="Skip this step").click()
    expect(page.get_by_text("You are set up")).to_be_visible()
    expect(page.locator(".progress")).to_contain_text("4 of 5 done, 1 skipped")
    v.check_page("welcome-done")

    # The server kept it: Today no longer offers the guide, and shows the watchlist filter.
    guide = v.api("GET", "/api/onboarding").json()
    assert guide["complete"] is True and guide["show"] is False
    v.go("/")
    expect(page.get_by_role("heading", level=1)).to_contain_text("Hello")
    expect(page.locator("app-setup-card section")).to_have_count(0)
    expect(page.get_by_label("Show fills and signals for")).to_be_visible()
    v.guard.assert_clean()


def test_an_admin_sees_whether_the_install_is_ready(browse, stack, viewport):
    v: Visit = browse(stack.admin)
    page = v.page
    v.go("/welcome")
    install = page.locator("section.system")
    expect(install).to_contain_text("Your install")
    expect(install).to_contain_text("Data source key")
    expect(install).to_contain_text("First data load")
    # The stack loaded prices, so the first load is done. Whether a backup
    # exists depends on the other journeys, so only check the item is there.
    expect(install.locator(".check", has_text="First data load")).to_have_attribute(
        "data-done", "true"
    )
    expect(install.locator(".check", has_text="Backups")).to_contain_text("on disk")
    v.check_page("welcome-admin")
    v.guard.assert_clean()


def test_a_trader_charts_a_ticker_from_a_watchlist(browse, stack, viewport):
    v: Visit = browse(stack.trader)
    page = v.page
    made = v.api(
        "POST",
        "/api/watchlists",
        data={"name": f"Charts {viewport}", "tickers": ["AAA.US", "BBB.US"]},
    )
    assert made.status == 201, made.text()
    wid = made.json()["id"]

    v.go("/charts")
    expect(page.get_by_text("Pick a ticker")).to_be_visible()
    page.get_by_label("Watchlist to jump between").select_option(wid)
    page.get_by_role("link", name="BBB.US").click()
    expect(page.get_by_role("heading", level=1)).to_have_text("BBB.US")

    chart = page.locator("app-price-chart")
    expect(chart.locator("canvas").first).to_be_visible()
    expect(chart.locator(".legend")).to_contain_text("C ")
    expect(chart.locator(".legend")).to_contain_text("MA 50")
    ranges = page.get_by_role("group", name="Range")
    ranges.get_by_role("button", name="3M").click()
    expect(ranges.get_by_role("button", name="3M")).to_have_attribute("aria-pressed", "true")
    expect(page.locator("section", has_text="Your fills")).to_be_visible()

    # Compare another ticker on one scale, then read the rolling Sharpe (13.5).
    page.get_by_label("Ticker to compare").fill("aaa.us")
    page.get_by_role("button", name="Compare", exact=True).click()
    expect(page).to_have_url(re.compile(r"vs=AAA\.US"))
    figures = page.locator(".figures-table")
    expect(figures).to_contain_text("BBB.US")
    expect(figures).to_contain_text("AAA.US")
    expect(page.get_by_role("button", name="Stop comparing AAA.US")).to_be_visible()
    perf = page.locator("section", has=page.get_by_role("heading", name="Rolling Sharpe"))
    expect(perf.locator("canvas").first).to_be_visible()
    perf.get_by_role("button", name="6M").click()
    expect(perf.get_by_role("button", name="6M")).to_have_attribute("aria-pressed", "true")
    v.check_page("chart")

    # Orders download as a CSV file over the session.
    v.go("/orders")
    with page.expect_download() as download:
        page.get_by_role("button", name="Orders CSV").click()
    assert download.value.suggested_filename.startswith("stonks-orders-")
    v.guard.assert_clean()
