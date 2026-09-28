"""Journeys for the roadmap 20.7 and 20.8 console screens:

- a trader reads the calendar for their holdings, a watchlist and some
  tickers, flips through earnings, ex-dividend dates, economic releases and
  news, and finds the upcoming-event alerts in Settings,
- the order ticket asks about earnings for its ticker,
- a trader runs a screen, saves it, stores it as a snapshot universe (with
  the survivorship warning) and deletes it.

The seeded stack has prices but no calendars or news, so the calendar shows
its empty states. Each test runs on desktop and on a 375px phone.
"""

from __future__ import annotations

import re

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


def test_a_trader_reads_the_calendar_and_news(browse, stack, viewport):
    v = _trader(browse, stack, "calendar", viewport)
    page = v.page
    made = v.api("POST", "/api/watchlists", data={"name": f"Cal {viewport}", "tickers": ["AAA.US"]})
    assert made.status == 201, made.text()

    v.open_nav()
    page.get_by_role("link", name="Calendar").click()
    expect(page.get_by_role("heading", level=1)).to_have_text("Calendar")
    expect(page.get_by_role("heading", name="Earnings reports")).to_be_visible()
    expect(page.get_by_text("No earnings in these days")).to_be_visible()
    v.check_page("calendar")

    show = page.get_by_role("radiogroup", name="Show")
    show.get_by_role("radio", name=re.compile(r"^Ex-dividend")).click()
    expect(page.get_by_role("heading", name="Ex-dividend dates")).to_be_visible()
    show.get_by_role("radio", name=re.compile(r"^Economic")).click()
    expect(page.get_by_label("Countries")).to_be_visible()
    page.get_by_label("Countries").fill("us")
    page.get_by_label("Countries").press("Enter")
    expect(page.get_by_text("No economic releases in these days")).to_be_visible()

    # A watchlist, then typed tickers.
    whose = page.get_by_role("radiogroup", name="Whose events")
    whose.get_by_role("radio", name="Watchlists").click()
    page.get_by_label("Watchlist", exact=True).select_option(label=f"Cal {viewport}")
    whose.get_by_role("radio", name="Tickers").click()
    expect(page.get_by_text("Name some tickers")).to_be_visible()
    page.get_by_label("Tickers", exact=True).fill("aaa.us, bbb.us")
    page.get_by_role("button", name="Show events").click()
    expect(page.get_by_text("No economic releases in these days")).to_be_visible()

    show.get_by_role("radio", name="News").click()
    expect(page.get_by_role("heading", name="News and sentiment")).to_be_visible()
    expect(page.get_by_text("No news yet")).to_be_visible()
    v.check_page("calendar-news")

    whose.get_by_role("radio", name="Everything").click()
    expect(page.get_by_text("Pick whose news to show")).to_be_visible()

    # The event alerts sit with the alert settings: one switch per kind, on
    # until turned off, and the economic release choices.
    v.go("/settings?tab=alerts")
    prefs = page.locator("app-notification-prefs")
    events = prefs.locator("fieldset", has=page.get_by_text("Upcoming events", exact=True))
    earnings = events.get_by_label("Earnings coming up")
    expect(earnings).to_be_checked()
    expect(events.get_by_label("Ex-dividend dates coming up")).to_be_checked()
    expect(events.get_by_label("Economic releases coming up")).to_be_checked()
    expect(prefs.get_by_label("Importance")).to_be_visible()
    with page.expect_response(
        lambda r: r.request.method == "PUT" and r.url.endswith("/api/notifications/preferences")
    ) as saved:
        earnings.uncheck()
    assert saved.value.ok, saved.value.status
    page.reload()
    expect(earnings).not_to_be_checked()
    v.guard.assert_clean()


def test_the_order_ticket_checks_earnings(browse, stack, viewport):
    v = _trader(browse, stack, "earnings", viewport)
    page = v.page
    v.go("/orders/new")
    expect(page.get_by_role("heading", name="New order")).to_be_visible()
    with page.expect_response(re.compile(r"/api/calendars/earnings-warnings\?")) as resp:
        page.get_by_label("Ticker").fill("aaa.us")
    assert resp.value.ok
    assert resp.value.json()["checked"] == ["AAA.US"]
    # No calendar data on this stack: no report, so no warning line.
    expect(page.locator("app-earnings-warning [role=status]")).to_have_count(0)
    v.guard.assert_clean()


def test_a_trader_screens_saves_and_makes_a_universe(browse, stack, viewport):
    v = _trader(browse, stack, "screener", viewport)
    page = v.page
    v.open_nav()
    page.get_by_role("link", name="Screener").click()
    expect(page.get_by_role("heading", level=1)).to_have_text("Screener")
    expect(page.get_by_text("No screens yet")).to_be_visible()
    v.check_page("screener")

    page.get_by_role("checkbox", name="Stocks").check()
    page.get_by_role("button", name="Add a filter").click()
    page.get_by_label("Metric 1").select_option(label="Price")
    page.get_by_label("At least").fill("1")
    page.get_by_label("Sort by").select_option(label="Price")
    page.get_by_role("button", name="Run screen").click()
    results = page.locator("section.results")
    expect(results).to_contain_text(re.compile(r"\d+ match(es)? out of \d+"))
    expect(results.get_by_role("link", name="AAA.US")).to_be_visible()
    v.check_page("screener-results")

    name = f"Stocks over 1 {viewport}"
    page.get_by_label("Screen name").fill(name)
    page.get_by_role("button", name="Save screen").click()
    saved = page.locator("section.saved li", has_text=name)
    expect(saved).to_be_visible()
    expect(page.get_by_role("button", name="Save changes")).to_be_disabled()

    page.get_by_role("button", name="Save as a universe").click()
    dialog = page.get_by_role("dialog")
    expect(dialog.get_by_role("heading", name="Save as a universe")).to_be_visible()
    dialog.get_by_role("radio", name="Today's matches").click()
    expect(dialog).to_contain_text("Survivorship bias.")
    v.check_page("screener-universe-sheet")
    dialog.get_by_label("Short name").fill(f"stocks-over-1-{viewport}")
    dialog.get_by_role("button", name="Save universe").click()
    universe = page.locator("section.universe")
    expect(universe).to_contain_text("Saved as a universe")
    expect(universe).to_contain_text("survivorship")
    stored = v.api("GET", f"/api/universes/stocks-over-1-{viewport}")
    assert stored.ok, stored.text()
    assert stored.json()["kind"] == "list"

    saved.get_by_role("button", name=f"Delete {name}").click()
    page.get_by_role("dialog").get_by_role("button", name="Delete screen").click()
    expect(page.locator("section.saved li", has_text=name)).to_have_count(0)
    assert v.api("GET", "/api/screener/screens").json()["items"] == []
    v.guard.assert_clean()
