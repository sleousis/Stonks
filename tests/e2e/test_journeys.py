"""User journeys through the real console against a real ``stonks serve``.

Each test runs on desktop and on a 375px phone (the ``viewport`` fixture).
Run with ``uv run pytest -m e2e tests/e2e`` (see docs/testing.md).
"""

from __future__ import annotations

import re

import pyotp
import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import Visit

pytestmark = pytest.mark.e2e


def test_first_sign_in_enrols_totp_then_signs_in_with_a_code(browse, stack, viewport):
    person = stack.add_person("trader", f"new-{viewport}@e2e.test", f"New {viewport.title()}")
    v: Visit = browse()
    page = v.go("/")
    expect(page).to_have_url(re.compile(r"/login"))
    v.check_page("login")

    page.get_by_label("Email").fill(person.email)
    page.get_by_label("Password").fill(person.password)
    page.get_by_role("button", name="Sign in").click()

    # Enrol: read the key shown next to the QR code, as a person typing it would.
    key = page.locator(".secret code")
    expect(key).to_be_visible()
    v.check_page("login-enrol")
    person.totp_secret = re.sub(r"\s+", "", key.inner_text())
    assert len(person.totp_secret) >= 16
    page.get_by_role("textbox", name="Code from the app").fill(person.code())
    page.get_by_role("button", name="Confirm").click()

    # Recovery codes are shown once; Continue waits for the checkbox.
    expect(page.get_by_role("heading", level=1)).to_contain_text(re.compile("recovery", re.I))
    codes = page.locator("app-one-time-secret")
    expect(codes).to_contain_text(re.compile(r"\w{4,}"))
    v.check_page("login-recovery-codes")
    cont = page.get_by_role("button", name="Continue")
    expect(cont).to_be_disabled()
    page.get_by_label("I saved my recovery codes").check()
    cont.click()

    # Alerts on this device: offered only where the browser can do push.
    heading = page.get_by_role("heading", level=1)
    not_now = page.get_by_role("button", name="Not now")
    expect(heading.or_(not_now).first).to_be_visible()
    if not_now.is_visible():
        not_now.click()
    expect(heading).to_contain_text(f"Hello, New {viewport.title()}")

    sign_out(v)
    expect(page).to_have_url(re.compile(r"/login"))

    page.get_by_label("Email").fill(person.email)
    page.get_by_label("Password").fill(person.password)
    page.get_by_role("button", name="Sign in").click()
    v.check_page("login-verify")
    page.get_by_role("textbox", name="Code").fill(person.code())
    page.get_by_role("button", name="Continue").click()
    expect(page.get_by_role("heading", level=1)).to_contain_text("Hello")
    assert pyotp.TOTP(person.totp_secret).now()


def sign_out(v: Visit) -> None:
    """Sign out sits in the account menu by the user's name (UX-10)."""
    v.open_nav()
    v.page.locator("app-account-menu summary:visible").click()
    v.page.get_by_role("button", name="Sign out").click()


def enrol(stack, person) -> None:
    """Give a seeded person a second factor, as the stack does for the admin."""
    from stonks.security.crypto import SecretBox
    from stonks.store.state import SqliteState

    box = SecretBox.from_env(stack.env)
    sealed = box.seal(person.totp_secret.encode(), aad=f"users.totp_secret:{person.user_id}")
    with SqliteState(stack.data_dir / "state.sqlite") as state:
        state.execute(
            "UPDATE users SET totp_secret_enc = ?, mfa_enrolled_at = '2026-01-01' WHERE id = ?",
            [sealed.token, person.user_id],
        )


def test_trader_home_shows_portfolio_signals_and_strategies(browse, stack, viewport):
    title = f"AAA.US buy signal ({viewport})"
    stack.notify(stack.trader, title, "buy_and_hold wants AAA.US at 20 percent.")
    v = browse(stack.trader)
    page = v.page
    expect(page.get_by_role("heading", name="My portfolio")).to_be_visible()
    expect(page.locator("app-portfolio-card")).to_contain_text("$4,321.00")
    expect(page.locator("app-signals-card")).to_contain_text(title)
    expect(page.get_by_role("heading", name="My strategies")).to_be_visible()
    v.check_page("home-trader")


def test_trader_home_lists_followed_strategies(browse, stack, viewport):
    v = browse(stack.trader)
    card = v.page.locator("app-strategies-card")
    expect(card.locator("app-loading-state")).to_have_count(0)
    expect(card).not_to_contain_text("Coming soon")
    expect(card).not_to_contain_text("Could not load")
    v.guard.assert_clean()


def test_new_trader_home_loads_without_errors(browse, stack, viewport):
    person = stack.add_person("trader", f"empty-{viewport}@e2e.test", "Empty Trader")
    person.totp_secret = pyotp.random_base32()
    enrol(stack, person)
    v = browse(person)
    card = v.page.locator("app-portfolio-card")
    expect(card.locator("app-loading-state")).to_have_count(0)
    v.page.wait_for_load_state("networkidle")
    expect(card).to_contain_text("No portfolio yet")
    expect(card).not_to_contain_text("Could not load", timeout=1_000)
    v.guard.assert_clean()


def test_notification_appears_in_feed_and_is_marked_read(browse, stack, viewport):
    title = f"Order filled ({viewport})"
    nid = stack.notify(stack.trader, title, "Bought 12 CCC.US.")
    v = browse(stack.trader)
    card = v.page.locator("app-signals-card")
    item = card.locator("li", has_text=title)
    expect(item).to_have_class(re.compile("unread"))
    card.get_by_role("button", name="Mark all read").click()
    expect(item).not_to_have_class(re.compile("unread"))
    expect(card.get_by_role("button", name="Mark all read")).to_have_count(0)
    feed = v.api("GET", "/api/notifications?limit=100").json()
    read = {i["id"]: i["read_at"] for i in feed["items"]}
    assert read[nid], "the feed item was not marked read on the server"
    v.check_page("home-signals-read")


def test_build_universe_from_csv_and_view_members(browse, stack, viewport, tmp_path):
    uid = f"e2e_csv_{viewport}"
    csv = tmp_path / "members.csv"
    csv.write_text("ticker\nAAA.US\nBBB.US\nCCC.US\n", encoding="utf-8")
    v = browse(stack.trader)
    page = v.go("/universes")
    expect(page.get_by_role("heading", name="Stored universes")).to_be_visible()
    v.check_page("universes")
    page.get_by_role("button", name="New universe").click()
    page.get_by_label("Id", exact=True).fill(uid)
    page.get_by_label("A CSV file").check()
    page.get_by_label("CSV file", exact=True).set_input_files(csv)
    v.check_page("universes-new")
    page.get_by_role("button", name="Create universe").click()

    # Creating opens the universe; its members appear after a refresh.
    expect(page.get_by_role("heading", level=1)).to_contain_text(uid)
    expect(page.locator("main")).to_contain_text("Not refreshed")
    v.check_page("universe-detail")
    page.get_by_role("button", name="Refresh", exact=True).click()
    page.get_by_role("dialog").get_by_role("button", name="Refresh").click()
    main = page.locator("main")
    expect(main).to_contain_text("succeeded", timeout=60_000)
    expect(main).to_contain_text("3 on ")
    for ticker in ("AAA.US", "BBB.US", "CCC.US"):
        expect(main).to_contain_text(ticker)
    v.check_page("universe-members")

    page = v.go("/universes")
    expect(page.get_by_role("link", name=uid)).to_be_visible()


def test_lab_run_with_quick_preset_shows_results(browse, stack, viewport):
    v = browse(stack.trader)
    page = v.go("/lab")
    page.get_by_role("tab", name="Lab run").click()
    form = page.locator("#lab-panel-lab_run")
    expect(form).to_be_visible()
    form.get_by_role("radio", name=re.compile(r"^momentum\b")).check()
    page.locator("#lr-tickers").fill("AAA.US,BBB.US,CCC.US")
    start = stack.market_end.replace(year=stack.market_end.year - 1)
    page.locator("#lr-start").fill(start.isoformat())
    page.locator("#lr-end").fill(stack.market_end.isoformat())
    form.get_by_role("radio", name=re.compile("^Quick")).check()
    v.check_page("lab-run-form")
    # A plain lab run starts at once: no confirm for research (UX-29).
    page.get_by_role("button", name="Start lab run").click()

    result = page.locator("section", has=page.get_by_role("heading", name="Result"))
    expect(result).to_contain_text(re.compile("pass|fail", re.I), timeout=180_000)
    v.check_page("lab-run-result")


def test_promote_gate_refuses_then_admin_overrides_and_trader_is_refused(browse, stack, viewport):
    from tests.e2e.stack import SHADOW_IDS

    sid = SHADOW_IDS[viewport]
    promote_url = rf"/api/strategies/{sid}/promote$"

    # A strategy that has not passed the go-live check offers no Go live
    # (UX-23). A trader sees why they cannot act, and the server refuses the
    # call anyway.
    trader = browse(stack.trader)
    trader.guard.expect_refusal(403, promote_url, "traders cannot promote")
    page = trader.go(f"/strategies/{sid}")
    expect(page.get_by_role("heading", level=1)).to_contain_text(sid)
    expect(page.get_by_role("button", name="Go live")).to_have_count(0)
    expect(page.get_by_role("button", name="Override…")).to_have_count(0)
    expect(page.locator("app-permission-note").first).to_be_visible()
    refused = trader.api("POST", f"/api/strategies/{sid}/promote", data={"reason": "trader tries"})
    assert refused.status == 403
    status = trader.api("GET", f"/api/strategies/{sid}").json()["status"]
    assert status == "shadow"

    admin = browse(stack.admin)
    page = admin.go(f"/strategies/{sid}")
    admin.check_page("strategy-detail")
    # An admin gets "Override…", which asks for the override straight away,
    # on the go-live ticket with the failed check.
    expect(page.get_by_role("button", name="Go live")).to_have_count(0)
    page.get_by_role("button", name="Override…").click()
    dialog = page.locator("dialog[open]")
    expect(dialog).to_contain_text("without passing the check")
    expect(dialog.locator("app-mode-stamp")).to_contain_text("PAPER")

    override = page.get_by_role("button", name="Override and go live")
    expect(override).to_be_visible()
    admin.check_page("strategy-promote-override")
    dialog = page.locator("dialog[open]")
    fill_status_dialog(dialog, "override", "Seeded e2e strategy, override recorded on purpose.")
    # The page always shows the lifecycle ladder, "Live" step included, so
    # wait for the promote call itself before reading the audit trail.
    with page.expect_response(
        lambda r: r.request.method == "POST" and re.search(promote_url, r.url) is not None
    ) as promoted:
        override.click()
    assert promoted.value.ok, promoted.value.status
    status = admin.api("GET", f"/api/strategies/{sid}").json()["status"]
    assert status == "active"
    history = admin.api("GET", f"/api/strategies/{sid}/history").json()
    rows = history if isinstance(history, list) else history["items"]
    assert any(r.get("override") and r.get("to_status") == "active" for r in rows), rows


def stack_cash() -> float:
    from tests.e2e.stack import TRADER_CASH

    return TRADER_CASH


def fill_status_dialog(dialog, typed: str, reason: str) -> None:
    """The status-change dialog: a reason and, when asked, a typed word."""
    reason_box = dialog.locator("textarea[id$='-reason'], input[id$='-reason']")
    typed_box = dialog.locator("input[id$='-typed']")
    expect(reason_box.or_(typed_box).first).to_be_visible()
    if reason_box.count():
        reason_box.fill(reason)
    if typed_box.count():
        typed_box.fill(typed)


def run_real_tick(v: Visit, as_of: str, ticker: str) -> None:
    page = v.go("/orders/ticks")
    page.get_by_label("Dry run").uncheck()
    page.get_by_label("As of").fill(as_of)
    page.get_by_role("textbox", name="Tickers", exact=True).fill(ticker)
    page.get_by_role("button", name="Start trading run", exact=True).click()
    dialog = page.get_by_role("dialog", name="Trading run ticket")
    dialog.get_by_role("textbox").fill("simulated")
    dialog.get_by_role("button", name="Start trading run").click()


def tick_result(v: Visit):
    result = v.page.locator(".result")
    expect(result).to_contain_text("Orders", timeout=90_000)
    return result


def test_paper_tick_places_orders_and_fills(browse, stack, viewport):
    ticker = {"desktop": "AAA.US", "phone": "BBB.US"}[viewport]
    as_of = stack.next_tick_date().isoformat()
    v = browse(stack.admin)
    run_real_tick(v, as_of, ticker)
    result = tick_result(v)
    expect(result.locator("dd").first).to_have_text("1")
    v.check_page("tick-result")
    expect(v.page.locator("app-session-strip .strip")).to_have_attribute("data-tone", "calm")

    page = v.go("/orders")
    expect(page.locator("main")).to_contain_text(ticker)
    v.check_page("orders")
    page = v.go("/orders/fills")
    expect(page.locator("main")).to_contain_text(ticker)
    v.check_page("fills")


def wait_until_second_factor_is_stale(v: Visit) -> None:
    for _ in range(40):
        if not v.api("GET", "/api/auth/me").json().get("mfa_fresh"):
            return
        v.page.wait_for_timeout(1_000)
    raise AssertionError("the second factor stayed fresh")


@pytest.fixture
def lift_halts_after(stack):
    """A failed kill switch journey must not leave trading halted for the rest."""
    yield
    from datetime import UTC, datetime

    from stonks.store.state import SqliteState

    with SqliteState(stack.data_dir / "state.sqlite") as state:
        state.execute(
            "UPDATE risk_halts SET cleared_at = ?, cleared_by = 'test:e2e',"
            " clear_reason = 'e2e cleanup' WHERE cleared_at IS NULL",
            [datetime.now(UTC).isoformat()],
        )


def test_kill_switch_blocks_orders_until_resumed_with_a_fresh_code(
    browse, stack, viewport, lift_halts_after
):
    v = browse(stack.admin)
    page = v.go("/ops/halts")
    v.check_page("halts")
    page.get_by_label("Reason").fill(f"e2e kill switch drill ({viewport})")
    page.get_by_role("button", name="Engage kill switch").click()
    page.locator("dialog[open]").get_by_role("button", name="Engage kill switch").click()
    strip = page.locator("app-session-strip .strip")
    expect(strip).not_to_have_attribute("data-tone", "calm")
    expect(page.get_by_role("region", name="Trading halted")).to_be_visible()
    v.check_page("halts-engaged")

    run_real_tick(v, stack.next_tick_date().isoformat(), "CCC.US")
    expect(tick_result(v).locator("dd").first).to_have_text("0")

    # Let the sign-in code go stale so Resume has to ask for a fresh one.
    wait_until_second_factor_is_stale(v)
    page = v.go("/ops/halts")
    page.get_by_role("button", name=re.compile("^Resume")).first.click()
    dialog = page.locator("dialog[open]")
    expect(dialog).to_contain_text("Resume trading?")
    fill_status_dialog(dialog, "RESUME TRADING", "Drill over, trading again.")
    dialog.get_by_role("button", name="Resume trading").click()
    step_up = page.locator(
        "dialog[open]", has=page.get_by_role("heading", name="Confirm it is you")
    )
    step_up.get_by_label("Code from your authenticator app").fill(stack.admin.code())
    step_up.get_by_role("button", name=re.compile("^(Confirm|Continue|Verify)")).click()
    expect(page.locator("main")).to_contain_text("Trading is not halted")
    expect(page.locator("app-session-strip .strip")).to_have_attribute("data-tone", "calm")


def test_stop_trading_from_the_strip_halts_the_picked_portfolio(
    browse, stack, viewport, lift_halts_after
):
    """UX-01: from any page, one tap on Stop trading opens the kill sheet,
    preset to the portfolio on screen, and the strip turns red at once."""
    v = browse(stack.trader)
    page = v.go("/strategies")
    strip = page.locator("app-session-strip .strip")
    stop = strip.get_by_role("button", name="Stop trading")
    expect(stop).to_be_visible()
    if v.phone:
        box = stop.bounding_box()
        assert box and box["height"] >= 44, box
    stop.click()

    sheet = page.get_by_role("dialog", name="Stop trading")
    expect(sheet).to_be_visible()
    expect(sheet.get_by_role("radio", name="Trader paper book")).to_be_checked()
    expect(sheet.get_by_role("radio", name="All new orders")).to_be_checked()
    expect(sheet.get_by_role("group", name="Kill switch")).to_contain_text("PAPER")
    v.check_page("kill-sheet")
    sheet.get_by_label("Reason").fill(f"e2e stop from the strip ({viewport})")
    sheet.get_by_role("button", name="Stop trading").click()

    expect(strip).to_have_attribute("data-tone", "kill")
    expect(page.get_by_role("region", name="Trading halted")).to_contain_text("Trader paper book")
    expect(strip.get_by_role("link", name="Resume trading on the Halts page")).to_be_visible()
    v.check_page("kill-strip")

    halts = v.api("GET", "/api/halts").json()
    rows = halts if isinstance(halts, list) else halts["items"]
    mine = [h for h in rows if h.get("reason") == f"e2e stop from the strip ({viewport})"]
    assert len(mine) == 1, rows
    assert mine[0]["scope"] == "portfolio"
    assert mine[0]["portfolio_id"] == stack.trader_portfolio


def test_admin_backs_up_now_and_lists_backups(browse, stack, viewport):
    v = browse(stack.admin)
    page = v.go("/ops/schedule")
    v.check_page("schedule")
    page.get_by_role("button", name="Back up now").click()
    page.locator("dialog[open]").get_by_role("button", name="Back up now").click()
    backups = page.locator("section", has=page.get_by_role("heading", name="Backups"))
    expect(backups).to_contain_text(re.compile("succeeded", re.I), timeout=120_000)
    v.check_page("schedule-backups")


def test_trader_cannot_see_the_admins_portfolio(browse, stack, viewport):
    from stonks.accounts.models import DEFAULT_PORTFOLIO_ID as admin_pf

    admin = browse(stack.admin)
    assert admin.api("GET", f"/api/portfolio?portfolio_id={admin_pf}").ok

    v = browse(stack.trader)
    v.guard.expect_refusal(404, r"/api/(portfolio|pnl)", "another person's portfolio")
    assert v.api("GET", f"/api/portfolio?portfolio_id={admin_pf}").status == 404
    assert v.api("GET", "/api/portfolio").json()["cash"] == pytest.approx(stack_cash())

    # The admin's tick bought AAA.US and BBB.US; none of it shows to the trader.
    page = v.go(f"/dashboard?portfolio_id={admin_pf}")
    main = page.locator("main")
    expect(main).to_contain_text("$4,321.00")
    expect(main).to_contain_text("0 positions")
    v.check_page("dashboard-trader")
    page = v.go("/orders")
    expect(page.locator("main")).to_contain_text("No orders yet")
    v.check_page("orders-trader")
    page = v.go("/")
    expect(page.locator("app-portfolio-card")).to_contain_text("$4,321.00")
