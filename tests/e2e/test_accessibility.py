"""axe-core on every page of the console, on desktop and on a phone.

One test per viewport signs in once and visits every route, so a failure
lists all pages with violations at once. The journeys also record axe
results for the states they reach (dialogs, results) in
``test-results/axe-report.json``.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import KNOWN_AXE, new_axe_violations, record_axe
from tests.e2e.stack import ACTIVE_IDS

pytestmark = pytest.mark.e2e

ADMIN_PAGES = (
    "/",
    "/profile",
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
    "/shadow",
    "/go-live",
    "/health",
    "/settings",
    "/ops/halts",
    "/ops/schedule",
    "/ops/data-quality",
    "/universes",
)
TRADER_PAGES = ("/", "/profile", "/settings", "/strategies")


def _audit(visit, pages, who: str) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in pages:
        page = visit.go(path)
        expect(page.get_by_role("heading", level=1)).to_be_visible()
        page.wait_for_load_state("networkidle")
        violations = new_axe_violations(page, record_axe(page, f"{who} {path}", visit.viewport))
        if violations:
            found[f"{who} {path}"] = [f"{v['id']} ({v['impact']})" for v in violations]
    return found


def test_every_page_has_no_axe_violations(browse, stack, viewport):
    found: dict[str, list[str]] = {}

    signed_out = browse()
    page = signed_out.go("/login")
    expect(page.get_by_role("heading", level=1)).to_be_visible()
    if violations := record_axe(page, "signed out /login", viewport):
        found["signed out /login"] = [v["id"] for v in violations]

    found |= _audit(browse(stack.admin), ADMIN_PAGES, "admin")
    found |= _audit(browse(stack.trader), TRADER_PAGES, "trader")
    assert not found, "axe violations:\n" + "\n".join(f"{k}: {v}" for k, v in found.items())


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
