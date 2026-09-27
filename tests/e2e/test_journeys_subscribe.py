"""Journey: a trader opens a paper portfolio on the profile, then follows a
strategy from its page and finds it on Today (roadmap 18.7).

Each test runs on desktop and on a 375px phone (the ``viewport`` fixture).
The follow is for signals only, so no book trades in the other journeys.
"""

from __future__ import annotations

import pyotp
import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import Visit
from tests.e2e.stack import ACTIVE_IDS
from tests.e2e.test_journeys import enrol

pytestmark = pytest.mark.e2e


def test_a_trader_opens_a_portfolio_and_follows_a_strategy(browse, stack, viewport):
    person = stack.add_person("trader", f"follow-{viewport}@e2e.test", "Follow Trader")
    person.totp_secret = pyotp.random_base32()
    enrol(stack, person)
    v: Visit = browse(person)
    page = v.page

    # A paper portfolio of their own, from the profile.
    v.go("/profile")
    panel = page.locator("app-portfolios-panel")
    expect(panel).to_contain_text("No portfolio yet")
    name = f"Swing {viewport}"
    panel.get_by_label("Name").fill(name)
    panel.get_by_role("button", name="Open portfolio").click()
    expect(panel.locator(".books li", has_text=name)).to_contain_text("PAPER")
    v.check_page("profile-portfolios")

    # Follow a live strategy from its page.
    sid = ACTIVE_IDS["AAA.US"]
    v.go(f"/strategies/{sid}")
    follow = page.locator("app-follow-panel")
    expect(follow.get_by_role("radio")).to_have_count(2)
    follow.get_by_label("Paper trading").check()
    expect(follow.get_by_label("Portfolio", exact=True)).to_contain_text(name)
    follow.get_by_label("Signals only").check()
    v.check_page("strategy-follow")
    follow.get_by_role("button", name="Follow").click()
    expect(follow).to_contain_text("You follow this strategy")
    expect(follow).to_contain_text("Signals only")

    # Today lists it with its switches.
    follow.get_by_role("link", name="Change it on Today").click()
    card = page.locator("app-strategies-card")
    expect(card.get_by_role("link", name=sid)).to_be_visible()
    subs = v.api("GET", "/api/subscriptions").json()["items"]
    assert [(s["strategy_id"], s["mode"]) for s in subs] == [(sid, "notify")]
    v.guard.assert_clean()
