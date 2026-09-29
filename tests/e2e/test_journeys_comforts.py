"""Journeys for roadmap 23.17, smaller comforts:

- a new trader opens the demo portfolio (labelled sample data), hides money
  amounts with one key while percentages stay, then removes the demo,
- a trader turns on a daily alert for a saved screen,
- a trader imports a CSV statement with a preview, sees a second import of
  the same file find only duplicates, and undoes the import.

Each test runs on desktop and on a 375px phone.
"""

from __future__ import annotations

import re

import pyotp
import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import Visit
from tests.e2e.test_journeys import enrol

pytestmark = pytest.mark.e2e

MASK = "•••••"
CSV = (
    "Date,Action,Symbol,Quantity,Price,Amount\n"
    "2026-01-02,DEPOSIT,,,,10000\n"
    "2026-01-05,BUY,AAA,10,20,-200\n"
)


def _trader(browse, stack, tag: str, viewport: str) -> Visit:
    person = stack.add_person("trader", f"{tag}-{viewport}@e2e.test", f"{tag.title()} Trader")
    person.totp_secret = pyotp.random_base32()
    enrol(stack, person)
    return browse(person)


def test_the_demo_and_privacy_mode(browse, stack, viewport):
    v = _trader(browse, stack, "demo", viewport)
    page = v.page
    v.go("/welcome")
    page.get_by_role("link", name="Open the demo portfolio").click()
    expect(page.get_by_role("heading", level=1)).to_have_text("Demo portfolio")
    page.get_by_role("button", name="Open the demo").click()
    expect(page.get_by_role("note")).to_contain_text("Sample data")
    expect(page.get_by_text("ACME.DEMO").first).to_be_visible()
    v.check_page("demo")

    tiles = page.locator("app-stat-tile")
    expect(tiles.first).to_contain_text("$")
    page.locator("main h1").click()
    page.keyboard.press("h")
    expect(tiles.first).to_contain_text(MASK)
    expect(tiles.nth(1)).to_contain_text("%")  # the return stays
    expect(page.locator("html")).to_have_attribute("data-privacy", "on")
    page.reload()
    expect(page.locator("app-stat-tile").first).to_contain_text(MASK)  # per device
    page.keyboard.press("Alt+Shift+H")
    expect(page.locator("app-stat-tile").first).not_to_contain_text(MASK)

    page.get_by_role("button", name="Remove the demo").click()
    page.get_by_role("dialog").get_by_role("button", name="Remove demo").click()
    expect(page.get_by_role("button", name="Open the demo")).to_be_visible()
    assert v.api("GET", "/api/demo").json()["exists"] is False
    v.guard.assert_clean()


def test_screen_alerts_and_a_csv_import(browse, stack, viewport):
    v = _trader(browse, stack, "comforts", viewport)
    page = v.page
    made = v.api(
        "POST",
        "/api/screener/screens",
        data={"name": f"Cheap {viewport}", "spec": {"sort_by": "price"}},
    )
    assert made.status == 201, made.text()

    v.go("/screener")
    alerts = page.locator("app-screen-alerts")
    expect(alerts.get_by_role("heading", name="Screen alerts")).to_be_visible()
    expect(alerts).to_contain_text("No alert.")
    alerts.get_by_label(f"When Cheap {viewport} alerts").select_option(label="Daily")
    expect(alerts).to_contain_text("Not run yet.")
    v.check_page("screener-alerts")

    v.go("/connections")
    page.get_by_role("link", name="Import a CSV statement").click()
    expect(page.get_by_role("heading", level=1)).to_have_text("Import a CSV statement")
    page.get_by_label("CSV file").set_input_files(
        {"name": "jan.csv", "mimeType": "text/csv", "buffer": CSV.encode()}
    )
    page.get_by_label("Portfolio name").fill(f"Old broker {viewport}")
    page.get_by_role("button", name="Preview").click()
    expect(page.get_by_text("A first guess from the headers")).to_be_visible()
    expect(page.get_by_role("status").filter(has_text="2 new")).to_be_visible()
    v.check_page("statement-import")
    page.get_by_role("button", name="Import 2 new rows").click()
    imports = page.locator("ul.imports")
    expect(imports).to_contain_text(re.compile(r"Old broker .*: 2 added"))

    # The same file again finds only rows already imported.
    page.get_by_role("button", name="Preview").click()
    expect(page.get_by_role("status").filter(has_text="0 new, 2 already imported")).to_be_visible()

    imports.get_by_role("button", name="Undo the import of jan.csv").click()
    page.get_by_role("dialog").get_by_role("button", name="Undo import").click()
    expect(imports).to_contain_text("Undone")
    listed = v.api("GET", "/api/statement-imports").json()["items"]
    assert listed[0]["undone_at"]
    v.guard.assert_clean()


def test_a_degiro_export_needs_no_mapping(browse, stack, viewport):
    """DEGIRO is never a connection: the trader follows its export link
    from Broker connections and imports the file without mapping columns."""
    from pathlib import Path

    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "degiro" / "transactions_nl.csv"
    v = _trader(browse, stack, "degiro", viewport)
    page = v.page
    v.go("/connections")
    panel = page.locator("app-broker-exports-panel")
    expect(panel.get_by_role("heading", name="Brokers without a connection")).to_be_visible()
    expect(panel).to_contain_text("Reads only")
    panel.get_by_role("link", name="Transactions").click()
    expect(page.get_by_role("heading", level=1)).to_have_text("Import a CSV statement")
    expect(page.get_by_label("What file is it?")).to_have_value("degiro_transactions")
    page.get_by_label("CSV file").set_input_files(
        {"name": "Transactions.csv", "mimeType": "text/csv", "buffer": fixture.read_bytes()}
    )
    page.get_by_label("Portfolio name").fill(f"DEGIRO {viewport}")
    page.get_by_label("Currency").fill("EUR")
    page.get_by_role("button", name="Preview").click()
    status = page.get_by_role("status").filter(has_text="Read as DEGIRO Transactions, Dutch.")
    expect(status).to_contain_text("2 new")
    expect(page.locator("#imp-col-date")).to_have_count(0)
    v.check_page("statement-import-degiro")
    page.get_by_role("button", name="Import 2 new rows").click()
    expect(page.locator("ul.imports")).to_contain_text(re.compile(r"DEGIRO .*: 2 added"))
    v.guard.assert_clean()
