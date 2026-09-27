"""axe-core on every page of the console, on desktop and on a phone, and
no sideways scroll at phone width.

One test per viewport signs in once and visits every route, so a failure
lists all pages with violations at once. The journeys also record axe
results for the states they reach (dialogs, results) in
``test-results/axe-report.json``.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import KNOWN_AXE, new_axe_violations, record_axe
from tests.e2e.stack import ACTIVE_IDS, Stack

pytestmark = pytest.mark.e2e

ADMIN_PAGES = (
    "/",
    "/profile",
    "/insights",
    "/insights/risk",
    "/lab/ledger",
    "/admin/users",
    "/dashboard",
    "/strategies",
    f"/strategies/{ACTIVE_IDS['AAA.US']}",
    "/studio",
    "/lab",
    "/data",
    "/orders",
    "/orders/fills",
    "/orders/ticks",
    "/paper",
    "/go-live",
    "/health",
    "/settings",
    "/ops/halts",
    "/ops/schedule",
    "/ops/data-quality",
    "/universes",
    "/welcome",
    "/watchlists",
    "/charts/AAA.US",
    "/leaderboard",
    f"/strategies/{ACTIVE_IDS['AAA.US']}/tearsheet",
    # Phase 19 and 20, and 22.6
    "/assistant",
    "/insights/cash-flows",
    "/insights/tax",
    "/profile/live/pf_default",
    "/tickets",
    "/orders/new",
    "/orders/drafts",
    "/notifications/price-alerts",
    "/ops/models",
    f"/strategies/{ACTIVE_IDS['AAA.US']}?tab=versions",
    # merged since: calendars, the screener, factors and the research loop
    "/calendar",
    "/screener",
    "/lab/factors",
    "/lab/research",
)
TRADER_PAGES = (
    "/",
    "/insights",
    "/insights/risk",
    "/profile",
    "/settings",
    "/strategies",
    "/welcome",
    "/watchlists",
    "/charts/AAA.US",
    "/leaderboard",
    "/assistant",
    "/insights/cash-flows",
    "/insights/tax",
    "/tickets",
    "/orders/new",
    "/orders/drafts",
    "/notifications/price-alerts",
    "/calendar",
    "/screener",
)


def _trader_pages(stack: Stack) -> tuple[str, ...]:
    """The trader's pages, with the live settings of their own portfolio."""
    return (*TRADER_PAGES, f"/profile/live/{stack.trader_portfolio}")


def _expect_live_settings_refusals(visit) -> None:
    """Live settings read the account profile, which is 404 until saved."""
    visit.guard.expect_refusal(404, r"/live/account-profile$", "no account profile saved yet")


def _audit(visit, pages, who: str) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in pages:
        page = visit.go(path)
        expect(page.get_by_role("heading", level=1)).to_be_visible()
        page.wait_for_load_state("networkidle")
        violations = new_axe_violations(page, record_axe(page, f"{who} {path}", visit.viewport))
        problems = [f"{v['id']} ({v['impact']})" for v in violations]
        if visit.phone:
            scroll, client = page.evaluate(
                "() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]"
            )
            if scroll > client:
                problems.append(f"scrolls sideways ({scroll}px in {client}px)")
        if problems:
            found[f"{who} {path}"] = problems
    return found


def test_every_page_has_no_axe_violations(browse, stack, viewport):
    found: dict[str, list[str]] = {}

    signed_out = browse()
    page = signed_out.go("/login")
    expect(page.get_by_role("heading", level=1)).to_be_visible()
    if violations := record_axe(page, "signed out /login", viewport):
        found["signed out /login"] = [v["id"] for v in violations]

    admin = browse(stack.admin)
    _expect_live_settings_refusals(admin)
    found |= _audit(admin, ADMIN_PAGES, "admin")
    trader = browse(stack.trader)
    _expect_live_settings_refusals(trader)
    found |= _audit(trader, _trader_pages(stack), "trader")
    assert not found, "axe or phone width problems:\n" + "\n".join(
        f"{k}: {v}" for k, v in found.items()
    )


# Skipped while KNOWN_AXE is empty: nothing is known to be broken.
@pytest.mark.skipif(not KNOWN_AXE, reason="no known axe violations")
@pytest.mark.parametrize(
    ("path", "rule"),
    [pytest.param(path, rule, id=f"{path}-{rule}") for path, rule in KNOWN_AXE]
    or [pytest.param("/", "none", id="none")],
)
def test_known_axe_violation_is_still_there(browse, stack, viewport, path, rule):
    """Fails (xfail) while the app still has the violation; passes once fixed,
    which is the cue to drop it from KNOWN_AXE. Some only show once a page
    has table rows, so not strict."""
    visit = browse(stack.admin)
    page = visit.go(path)
    expect(page.get_by_role("heading", level=1)).to_be_visible()
    page.wait_for_load_state("networkidle")
    found = [v["id"] for v in record_axe(page, f"admin {path} (known)", viewport)]
    if rule in found:
        pytest.xfail(KNOWN_AXE[(path, rule)])
