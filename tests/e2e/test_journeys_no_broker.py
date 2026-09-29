"""Journey: a solo owner with no broker gets a strategy's signals on
Telegram, and nothing trades (docs/without-a-broker.md).

This module runs its own stack: no broker connection provider turned on,
no IB Gateway, the simulated broker, no strategy approved yet, a Telegram
bot token whose sends land in a file (:mod:`tests.e2e.fake_telegram`), and
the in-process scheduler, so ``stonks serve`` delivers notifications as it
does for a single-process install. The admin builds a Studio draft from a
template, puts it on trial, approves it with the override, follows it in
Alerts only, links Telegram, keeps signal alerts on Telegram only, starts a
trading run and then finds the signal in the feed and in Telegram.

Each test runs on desktop and on a 375px phone (the ``viewport`` fixture).
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import RESULTS_DIR, Visit
from tests.e2e.fake_telegram import LOG_ENV, read_sent
from tests.e2e.stack import DEFAULT_DIST, Stack, build_stack, start_server, stop_server
from tests.e2e.test_journeys import fill_status_dialog

pytestmark = pytest.mark.e2e

CHAT = "5151"
#: Only a job that talks to broker connections: it runs and finds none. No
#: scheduled tick can land in the middle of a journey.
SCHEDULER = """backend = "in_process"
delivery_interval_seconds = 0.5

[[scheduler.jobs]]
name = "connections_sync"
action = "connections_sync"
trigger = { type = "interval", every_minutes = 60 }
"""


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Stack]:
    """Overrides the shared stack for this module: an install with no
    broker of any kind and a Telegram bot."""
    dist = Path(os.environ.get("STONKS_E2E_DIST", DEFAULT_DIST))
    if not (dist / "index.html").is_file():
        pytest.fail(f"no built console at {dist}: run `npm ci && npm run build` in web/")
    root = tmp_path_factory.mktemp("no-broker")
    built = build_stack(
        root,
        dist=dist,
        seed_strategies=False,
        env={
            "STONKS_CONNECTIONS_ENABLED_PROVIDERS": None,
            "STONKS_TELEGRAM_BOT_TOKEN": "1:e2e-no-broker",
            LOG_ENV: str(root / "telegram.jsonl"),
        },
        scheduler=SCHEDULER,
    )
    start_server(built)
    try:
        yield built
    finally:
        stop_server(built)
        if built.log_path is not None:
            RESULTS_DIR.mkdir(parents=True, exist_ok=True)
            (RESULTS_DIR / "server-no-broker.log").write_bytes(built.log_path.read_bytes())


def _state(stack: Stack):
    from stonks.store.state import SqliteState

    return SqliteState(stack.data_dir / "state.sqlite")


def _telegram(stack: Stack, contains: str, timeout: float = 45.0) -> list[dict]:
    """The messages the server sent to Telegram, once one contains ``contains``."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        sent = read_sent(Path(stack.env[LOG_ENV]))
        if any(contains in m["text"] for m in sent):
            return sent
        time.sleep(0.25)
    raise AssertionError(f"no Telegram message with {contains!r}: {sent}")


def test_a_solo_owner_gets_signals_on_telegram_with_no_broker(browse, stack, viewport):
    v: Visit = browse(stack.admin)
    page = v.page
    name = f"Trend alerts {viewport}"
    chat = f"{CHAT}{len(viewport)}"

    # No broker anywhere: nothing to connect, no gateway.
    assert v.api("GET", "/api/brokers/gateways").json()["configured"] is False
    assert v.api("GET", "/api/brokers").json()["kind"] == "simulated"

    # 1. A Studio draft from a template, tested on price data, put on trial.
    v.go("/studio")
    page.get_by_role("button", name="New draft").click()
    page.get_by_role("radio", name=re.compile(r"^SMA trend following")).check()
    page.locator("#new-name").fill(name)
    page.get_by_role("button", name="Create draft").click()
    page.get_by_role("tab", name=re.compile("Test")).click()
    page.locator("#t-tickers").fill("AAA.US, BBB.US, CCC.US")
    start = stack.market_end.replace(year=stack.market_end.year - 1)
    page.locator("#t-start").fill(start.isoformat())
    page.locator("#t-end").fill(stack.market_end.isoformat())
    page.get_by_role("button", name="Run backtest").click()
    expect(page.locator("app-backtest-result")).to_be_visible(timeout=120_000)
    v.check_page("no-broker-studio-backtest")
    page.get_by_role("tab", name=re.compile("Ship")).click()
    ship = page.locator("app-draft-ship")
    ship.get_by_role("button", name="Put on trial").click()
    page.get_by_role("dialog").get_by_role("button", name="Put on trial").click()
    expect(ship).to_contain_text("on trial")
    sid = next(
        d["registered_strategy_id"]
        for d in v.api("GET", "/api/studio/drafts").json()["items"]
        if d["name"] == name
    )
    assert v.api("GET", f"/api/strategies/{sid}").json()["status"] == "shadow"

    # 2. Approve it with the override: the go-live check has no trial days.
    v.go(f"/strategies/{sid}")
    page.get_by_role("button", name="Override…").click()
    dialog = page.locator("dialog[open]")
    expect(dialog).to_contain_text("No real money moves")
    fill_status_dialog(dialog, "override", "Alerts only for me: nothing follows it with money.")
    with page.expect_response(
        lambda r: r.request.method == "POST" and r.url.endswith(f"/api/strategies/{sid}/promote")
    ) as promoted:
        page.get_by_role("button", name="Override and approve").click()
    assert promoted.value.ok, promoted.value.status

    # 3. Follow it in Alerts only on the Simulated default portfolio. The
    # approval made that portfolio follow it in Paper: switch it.
    v.go(f"/strategies/{sid}")
    control = page.locator("app-follow-panel app-follow-control")
    expect(control).to_be_visible()
    control.get_by_role("radio", name="Alerts only").check()
    expect(control.get_by_role("radio", name="Alerts only")).to_be_checked()
    v.check_page("no-broker-follow")
    subs = [
        s for s in v.api("GET", "/api/subscriptions").json()["items"] if s["strategy_id"] == sid
    ]
    assert [(s["portfolio_id"], s["mode"]) for s in subs] == [("pf_default", "notify")]

    # 4. Link Telegram with a one-time code and keep signals on it only.
    if v.api("GET", "/api/telegram/link").json()["linked"]:  # the other viewport's chat
        assert v.api("DELETE", "/api/telegram/link").status == 204
    v.go("/notifications/settings")
    panel = page.locator("app-telegram-link")
    panel.get_by_role("button", name="Get a link code").click()
    expect(panel).to_contain_text("/link ")
    code = re.search(r"/link (\S+)", panel.inner_text()).group(1)  # type: ignore[union-attr]
    with _state(stack) as state:  # what the bot does with "/link CODE"
        from stonks.telegram.links import LinkStore

        LinkStore(state).redeem(code, chat, f"owner_{viewport}")
    panel.get_by_role("button", name="Check the link").click()
    expect(panel).to_contain_text("Linked")
    grid = page.locator("app-notification-prefs")
    push = grid.get_by_role("checkbox", name="Signals by Push")
    if push.is_checked():
        push.uncheck()
    expect(push).not_to_be_checked()
    expect(grid.get_by_role("checkbox", name="Signals by Telegram")).to_be_checked()
    expect(grid.get_by_role("checkbox", name="Signals by Email")).not_to_be_checked()
    v.check_page("no-broker-alert-settings")

    # 5. A trading run.
    v.go("/orders/ticks")
    page.get_by_label("Dry run").uncheck()
    page.get_by_label("As of").fill(stack.market_end.isoformat())
    page.get_by_role("button", name="Start paper run", exact=True).click()
    dialog = page.get_by_role("dialog", name="Trading run ticket")
    dialog.get_by_role("textbox").fill("simulated")
    dialog.get_by_role("button", name="Start paper run").click()
    expect(page.locator(".result")).to_contain_text("Orders", timeout=90_000)

    # 6. The signal is in the feed...
    feed = v.api("GET", "/api/notifications").json()["items"]
    signals = [n for n in feed if n["category"] == "signal" and sid in (n["message"] or "")]
    assert signals, feed
    title = signals[0]["title"]
    v.go("/notifications")
    expect(page.locator("main")).to_contain_text(title)
    v.check_page("no-broker-feed")

    # ...and reached Telegram, only Telegram, with a link to the strategy.
    sent = _telegram(stack, title)
    mine = [m for m in sent if m["chat_id"] == chat and title in m["text"]]
    assert mine and f"/strategies/{sid}" in mine[0]["text"], sent
    with _state(stack) as state:
        rows = state.sql(
            "SELECT d.channel, d.status FROM notification_deliveries d"
            " JOIN notification_outbox o ON o.id = d.notification_id"
            " WHERE o.category = 'signal' AND o.strategy_id = ?",
            [sid],
        )
        orders = state.sql("SELECT COUNT(*) FROM orders")[0][0]
        fills = state.sql("SELECT COUNT(*) FROM fills")[0][0]
        connections = state.sql("SELECT COUNT(*) FROM broker_connections")[0][0]
        halts = state.sql("SELECT COUNT(*) FROM risk_halts WHERE cleared_at IS NULL")[0][0]
    assert {r["channel"] for r in rows} == {"telegram"}, [dict(r) for r in rows]
    # Nothing traded and no broker was reached.
    assert (orders, fills, connections, halts) == (0, 0, 0, 0)
    v.guard.assert_clean()
