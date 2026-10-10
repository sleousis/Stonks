"""Journey for the roadmap 20.10 Universes page:

- a trader goes from Data to Universes, makes a list universe by name
  (it fills its members at once, R22), reads its members and membership
  history, edits it (a list refills on save) and fetches the missing
  prices with the job followed to its result,
- the lab run form links a picked universe to its page,
- an admin deletes it after typing its id.

Each test runs on desktop and on a 375px phone.
"""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e


def test_a_trader_builds_edits_and_fills_a_universe_then_an_admin_deletes_it(
    browse, stack, viewport
):
    # R22: the id is made from the name.
    uid = f"edit-me-{viewport}"
    v = browse(stack.trader)
    page = v.go("/data")
    page.get_by_role("link", name="Universes", exact=True).click()
    expect(page.get_by_role("heading", level=1)).to_have_text("Universes")

    # A list from typed tickers.
    page.get_by_role("button", name="New universe").click()
    page.get_by_label("Name", exact=True).fill(f"Edit me {viewport}")
    page.get_by_role("textbox", name="Tickers", exact=True).fill("aaa.us, bbb.us")
    v.check_page("universes-create-list")
    page.get_by_role("button", name="Create universe").click()
    expect(page.get_by_role("heading", level=1)).to_contain_text(f"Edit me {viewport}")
    expect(page).to_have_url(re.compile(rf"/universes/{uid}"))

    # A list fills its members at once, with no Refresh to press.
    expect(page.get_by_label("Refresh result")).to_be_visible(timeout=60_000)
    members = page.locator("section", has=page.get_by_role("heading", name="Members on a date"))
    expect(members).to_contain_text("2 on ")
    history = page.locator("section", has=page.get_by_role("heading", name="Membership history"))
    expect(history).to_contain_text("AAA.US")
    expect(history).to_contain_text("Still a member")
    v.check_page("universe-history")

    # Edit: the same form. Saving a list refills its members at once.
    page.get_by_role("button", name="Edit", exact=True).click()
    edit = page.locator("#edit-panel")
    expect(edit.get_by_label("Name", exact=True)).to_have_value(f"Edit me {viewport}")
    edit.get_by_role("textbox", name="Tickers", exact=True).fill("AAA.US, BBB.US, CCC.US")
    v.check_page("universe-edit")
    edit.get_by_role("button", name="Save changes").click()
    expect(page.locator("#edit-panel")).to_have_count(0)
    expect(members).to_contain_text("3 on ", timeout=60_000)
    expect(page.get_by_text("The definition changed after the last refresh")).to_have_count(0)

    # Fetch the missing prices over the last month, followed to its result.
    start = stack.market_end - timedelta(days=30)
    page.locator("#e-start").fill(start.isoformat())
    page.locator("#e-end").fill(stack.market_end.isoformat())
    page.get_by_role("button", name="Fetch missing data", exact=True).click()
    page.get_by_role("dialog").get_by_role("button", name="Fetch missing data").click()
    result = page.get_by_label("Fetch missing data result")
    expect(result).to_be_visible(timeout=90_000)
    expect(result).to_contain_text("Requested")
    v.check_page("universe-ensure")

    # The lab run form links the picked universe to its page.
    page = v.go("/lab")
    page.locator("#lab-view-advanced").click()
    page.get_by_role("tab", name="Lab run").click()
    page.locator("#lr-universe").select_option(uid)
    link = page.get_by_role("link", name="See its members")
    expect(link).to_have_attribute("href", f"/universes/{uid}")

    # Only admins delete, after typing the id.
    admin = browse(stack.admin)
    page = admin.go(f"/universes/{uid}")
    page.get_by_role("button", name="Delete", exact=True).click()
    dialog = page.get_by_role("dialog")
    confirm = dialog.get_by_role("button", name="Delete universe")
    expect(confirm).to_be_disabled()
    dialog.locator("#confirm-typed").fill(uid)
    confirm.click()
    expect(page).to_have_url(re.compile(r"/universes$"))
    expect(page.get_by_role("link", name=f"Edit me {viewport}")).to_have_count(0)
