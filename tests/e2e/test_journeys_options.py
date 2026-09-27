"""Journey for the roadmap 17.6 options research page: a trader opens
Options under Research, sees that nothing trades options, reads the chain
of AAA.US (generated chains the stack seeds), switches to puts, draws the
payoff of a bull call spread and runs an options backtest.

Runs on desktop and on a 375px phone, where the chain turns into cards.
"""

from __future__ import annotations

import re

import pyotp
import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import Visit
from tests.e2e.test_journeys import enrol

pytestmark = pytest.mark.e2e


def _trader(browse, stack, viewport: str) -> Visit:
    person = stack.add_person("trader", f"options-{viewport}@e2e.test", "Options Trader")
    person.totp_secret = pyotp.random_base32()
    enrol(stack, person)
    return browse(person)


def test_a_trader_researches_options(browse, stack, viewport):
    v = _trader(browse, stack, viewport)
    page = v.page

    v.open_nav()
    page.get_by_role("link", name="Options", exact=True).click()
    expect(page.get_by_role("heading", level=1)).to_have_text("Options research")
    expect(page.get_by_role("note").first).to_contain_text("Research only, nothing trades options")
    expect(page.get_by_label("Underlying", exact=True)).to_have_value("AAA.US")
    expect(page.get_by_text("Generated chains, not market quotes")).to_be_visible()
    expect(page.get_by_text(re.compile(r"Expiry \d{4}-\d{2}-\d{2}, \d+ days out"))).to_be_visible()
    v.check_page("options")

    show = page.get_by_role("radiogroup", name="Show calls, puts or both")
    show.get_by_role("radio", name="Puts").click()
    expect(show.get_by_role("radio", name="Puts")).to_have_attribute("aria-checked", "true")

    page.get_by_label("Structure").select_option(label="Bull call spread")
    page.get_by_label("Days to expiry").fill("30")
    page.get_by_role("button", name="Draw payoff").click()
    diagram = page.locator("app-payoff-diagram")
    expect(diagram.get_by_role("img")).to_have_attribute(
        "aria-label", re.compile(r"^Bull call spread on AAA\.US: Debit")
    )
    expect(diagram.get_by_text(re.compile(r"^Buy 1 call"))).to_be_visible()
    expect(diagram.get_by_text(re.compile(r"^Sell 1 call"))).to_be_visible()
    v.check_page("options-payoff")

    page.get_by_label("Strategy", exact=True).select_option("vertical_spread")
    page.get_by_label("Run the validation checks").uncheck()
    page.get_by_role("button", name="Run backtest").click()
    expect(page.get_by_role("heading", name="vertical_spread on AAA.US")).to_be_visible(
        timeout=90_000
    )
    expect(page.get_by_text("never evidence for the strategy")).to_be_visible()
    v.check_page("options-backtest")
