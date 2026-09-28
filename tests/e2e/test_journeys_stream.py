"""Live engine journey (roadmap 21.3.4): the admin opens the Live engine
page from the nav. With no engine it says intraday trading is off. Then two
engines report: one running on an always-open market, one silent for ten
minutes. The page shows each with its stream, speed, failed steps and the
alarm. Runs on desktop and on a 375px phone.

The engines are status rows, as an engine process would write them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from playwright.sync_api import expect

from stonks.engine.monitor import LatencyHistogram
from stonks.engine.status import EngineStatusStore
from stonks.store.state import SqliteState
from stonks.streaming.health import StreamHealth
from tests.e2e.stack import Stack

pytestmark = pytest.mark.e2e


def _clear_engines(stack: Stack) -> None:
    with SqliteState(stack.data_dir / "state.sqlite") as state:
        state.execute("DELETE FROM engine_status")


def _report(stack: Stack, engine_id: str, *, silent_for: timedelta) -> None:
    now = datetime.now(UTC)
    health = StreamHealth(source="replay", state="streaming", bars_written=42, late_ticks=3)
    health.last_event_at = now - timedelta(seconds=2)
    lag, orders = LatencyHistogram(), LatencyHistogram()
    for v in (0.03, 0.04, 0.2):
        lag.observe(v)
    orders.observe(0.4)
    store = EngineStatusStore(stack.data_dir / "state.sqlite")
    store.start(engine_id, calendar="24/7", now=now - timedelta(hours=1))
    store.write(
        {
            "engine_id": engine_id,
            "calendar": "24/7",
            "state": "streaming",
            "last_dispatch_at": (now - silent_for).isoformat(),
            "driver": {
                "bar_closes": 40,
                "bars": 80,
                "late_bars": 0,
                "handler_errors": {"decide": 2} if silent_for else {},
            },
            "stream": health.snapshot(),
            "latency": {"dispatch_lag": lag.as_dict(), "event_to_order": orders.as_dict()},
        },
        now=now,
    )


def test_the_admin_watches_the_live_engine(browse, stack, viewport):
    _clear_engines(stack)
    v = browse(stack.admin)
    page = v.page

    v.open_nav()
    page.get_by_role("link", name="Live engine").click()
    expect(page.get_by_role("heading", level=1)).to_have_text("Live engine")
    expect(page.get_by_text("No live engine running")).to_be_visible()
    expect(page.get_by_text("Intraday trading is off")).to_be_visible()
    expect(page.get_by_role("heading", name="Intraday P&L by portfolio")).to_be_visible()
    v.check_page("live engine, none running")

    _report(stack, f"steady-{viewport}", silent_for=timedelta(0))
    _report(stack, f"quiet-{viewport}", silent_for=timedelta(minutes=10))
    page.get_by_role("button", name="Refresh").click()

    steady = page.get_by_role("region", name=f"steady-{viewport}")
    expect(steady).to_contain_text("Running")
    expect(steady).to_contain_text("Connected")
    expect(steady).to_contain_text("Median under 50 ms, 95% under 250 ms")
    expect(steady).to_contain_text("Late prices dropped")

    quiet = page.get_by_role("region", name=f"quiet-{viewport}")
    expect(quiet).to_contain_text("Silent")
    expect(quiet).to_contain_text("Steps that failed")
    expect(quiet).to_contain_text("decide")
    expect(page.get_by_role("status").filter(has_text="engine needs")).to_be_visible()
    v.check_page("live engine, one silent")
    _clear_engines(stack)
