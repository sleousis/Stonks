"""Insights journeys (roadmap 18.7): a trader opens their own book's
insights, sees the allocation, returns, which strategies agree with each
holding and the snapshot history, then the Risk screen with each measure
against its limit. Runs on desktop and on a 375px phone.

Each run uses its own trader with a seeded holding, so the shared trader's
home figures stay as the other journeys expect them.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pyotp
import pytest
from playwright.sync_api import expect

from stonks.accounts import PortfolioRepository, Scope
from stonks.accounts.users import UserRepository
from stonks.store.state import SqliteState
from tests.e2e.conftest import Visit
from tests.e2e.stack import Person, Stack

pytestmark = pytest.mark.e2e


def _investor(stack: Stack, viewport: str) -> Person:
    """A trader with a second factor, one paper book holding AAA.US and cash."""
    from stonks.security.crypto import SecretBox

    person = stack.add_person("trader", f"insights-{viewport}@e2e.test", "Ivy Investor")
    person.totp_secret = pyotp.random_base32()
    box = SecretBox.from_env(stack.env)
    sealed = box.seal(person.totp_secret.encode(), aad=f"users.totp_secret:{person.user_id}")
    day = stack.market_end - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    with SqliteState(stack.data_dir / "state.sqlite") as state:
        state.execute(
            "UPDATE users SET totp_secret_enc = ?, mfa_enrolled_at = '2026-01-01' WHERE id = ?",
            [sealed.token, person.user_id],
        )
        user = UserRepository(state).get(person.user_id)
        pid = PortfolioRepository(state).create(Scope.for_user(user), name="Ivy book").id
        for i, (as_of, positions) in enumerate(
            ((day - timedelta(days=7), {}), (day, {"AAA.US": 10.0}))
        ):
            tick_id = f"tick-e2e-insights-{viewport}-{i}"
            state.execute(
                "INSERT INTO tick_runs (id, started_at, finished_at, status) VALUES (?, ?, ?, 'ok')",
                [tick_id, f"{as_of}T21:00:00+00:00", f"{as_of}T21:01:00+00:00"],
            )
            state.execute(
                "INSERT INTO portfolio_snapshots (tick_id, portfolio_id, as_of, taken_at, cash,"
                " positions_json, total_value) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    tick_id,
                    pid,
                    as_of.isoformat(),
                    f"{as_of}T21:00:00+00:00",
                    1000.0,
                    json.dumps(positions),
                    2000.0,
                ],
            )
    return person


def _open(v: Visit, label: str) -> None:
    v.open_nav()
    v.page.get_by_role("link", name=label, exact=True).first.click()


def test_a_trader_reads_insights_on_their_own_book(browse, stack, viewport):
    person = _investor(stack, viewport)
    v = browse(person)
    page = v.page
    _open(v, "Insights")
    expect(page.get_by_role("heading", level=1)).to_have_text("Insights")

    alloc = page.locator(".slices")
    expect(alloc).to_contain_text("equity")
    page.get_by_role("radio", name="Holding", exact=True).click()
    expect(alloc).to_contain_text("AAA.US")

    expect(page.get_by_role("heading", name="Returns", exact=True)).to_be_visible()
    # The monthly returns heatmap, time weighted (13.7).
    months = page.locator("section", has=page.get_by_role("heading", name="Monthly returns"))
    expect(months.locator("table")).to_be_visible()
    expect(months.locator("caption")).to_contain_text("deposits and withdrawals left out")
    agreement = page.locator(".holdings")
    expect(agreement).to_contain_text("AAA.US")
    expect(agreement).to_contain_text("bah_aaa")
    history = page.get_by_role("heading", name="Portfolio history")
    expect(history).to_be_visible()
    expect(page.get_by_text("2 snapshots")).to_be_visible()
    expect(page.get_by_role("heading", name="All portfolios")).to_have_count(0)
    v.check_page("insights")

    page.get_by_role("link", name="Risk", exact=True).click()
    expect(page.get_by_role("heading", level=1)).to_have_text("Risk")
    limits = page.locator(".limits")
    expect(limits).to_contain_text("Largest holding")
    expect(limits).to_contain_text("No limit set")
    expect(page.get_by_role("heading", name="On a bad day")).to_be_visible()
    expect(page.get_by_text("No risk readings yet")).to_be_visible()
    v.check_page("insights-risk")


def test_an_admin_sees_totals_across_every_book(browse, stack, viewport):
    v = browse(stack.admin)
    page = v.go("/insights")
    expect(page.get_by_role("heading", name="All portfolios")).to_be_visible()
    # With fewer than three other owners the sums are held back and the card
    # says so. Other journeys open portfolios, so which one shows depends on
    # the order: either is right, zeros never are.
    totals = page.locator("section", has=page.get_by_role("heading", name="All portfolios"))
    held = "Totals appear once three or more traders have live money."
    shown = "Sums only. Admins never see anyone's holdings."
    expect(totals.get_by_text(held).or_(totals.get_by_text(shown))).to_be_visible()
    expect(totals).not_to_contain_text("$0.00")
    v.check_page("insights-admin")
