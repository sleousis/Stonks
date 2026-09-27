"""Journeys for the research screens (roadmap 22.2, 22.3, 22.5, 22.9):

- a trader finds a factor in the library, reads it, ranks names by it on a
  date, and checks a formula of their own,
- the research sessions page explains how sessions start when there are none.

Each test runs on desktop and on a 375px phone (the ``viewport`` fixture).
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import Visit
from tests.e2e.fake_market import EQUITIES

pytestmark = pytest.mark.e2e


def test_a_trader_reads_a_factor_and_checks_a_formula(browse, stack, viewport):
    v: Visit = browse(stack.trader)
    page = v.go("/lab/factors")
    expect(page.get_by_role("heading", name="Factor library")).to_be_visible()
    table = page.locator("app-data-table")
    expect(table).to_contain_text("mom_12_1")
    v.check_page("factors")

    # Narrow to the classic set, then search.
    page.locator("#factor-set").select_option("classic")
    expect(page).to_have_url(re.compile(r"set=classic"))
    page.locator("#factor-search").fill("reversal")
    expect(table).to_contain_text("reversal_1m")
    expect(table).not_to_contain_text("mom_12_1")

    # One factor: what it measures, then its values on the last market day.
    page.locator("#factor-search").fill("")
    page.get_by_role("link", name="mom_12_1", exact=True).click()
    expect(page.get_by_role("heading", name="mom_12_1")).to_be_visible()
    expect(page.get_by_text("Higher is better")).to_be_visible()
    values = page.locator("section", has=page.get_by_role("heading", name="Values on a date"))
    values.get_by_role("radio", name="Tickers").check()
    values.get_by_role("textbox", name="Tickers").fill(" ".join(EQUITIES))
    values.get_by_label("On the close of").fill(stack.market_end.isoformat())
    values.get_by_role("button", name="Show values").click()
    expect(values.locator("app-data-table")).to_contain_text(EQUITIES[0])
    v.check_page("factor-detail")

    # A formula of your own, checked as you type.
    page.get_by_role("link", name="Edit as a formula").click()
    expect(page.get_by_role("heading", name="Your formula")).to_be_visible()
    status = page.locator("#formula-status")
    expect(status).to_contain_text("Looks good.")
    page.locator("#formula-text").fill("Ref($close, -1)")
    expect(status).not_to_contain_text("Looks good.")
    page.locator("#formula-text").fill("$close / Ref($close, 20) - 1")
    expect(status).to_contain_text("20 bars")
    expect(page.get_by_role("heading", name="Tear sheet")).to_be_visible()
    v.check_page("factor-formula")
    v.guard.assert_clean()


def test_research_sessions_start_empty_with_a_start_form(browse, stack, viewport):
    v: Visit = browse(stack.trader)
    page = v.go("/lab/research")
    expect(page.get_by_role("heading", level=1)).to_be_visible()
    expect(page.get_by_role("link", name="Research sessions")).to_have_attribute(
        "aria-current", "page"
    )
    expect(page.get_by_label("What to look for")).to_be_visible()
    expect(page.get_by_text("No research sessions yet")).to_be_visible()
    v.check_page("research-sessions")
    v.guard.assert_clean()
