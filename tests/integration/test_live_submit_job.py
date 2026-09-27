"""The ``live_submit`` scheduler action (roadmap 19.8) on every backend: it
skips while no ticket is open, and a ticket it cannot send (a simulated
portfolio has no broker) is reported, never raised."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from stonks.config import Settings
from stonks.core.types import Order
from stonks.production.tickets import SubmitWindow, list_tickets, write_tickets
from stonks.scheduling.api_backend import API_ACTIONS
from stonks.scheduling.in_process import IN_PROCESS_ACTIONS
from stonks.scheduling.jobs import JobSpec, RunContext
from stonks.scheduling.local import LOCAL_ACTIONS
from stonks.scheduling.triggers import Fire, SessionTrigger
from stonks.store.state import SqliteState

AT = datetime(2026, 3, 18, 13, 10, tzinfo=UTC)


def _settings(tmp_path) -> Settings:
    s = Settings(state={"path": tmp_path / "state.sqlite"}, notify={"backends": []})
    with SqliteState(s.state.path) as state:
        state.migrate()
    return s


def _ctx(settings) -> RunContext:
    return RunContext(
        spec=JobSpec("live_submit", "live_submit", SessionTrigger("XNYS", "open", timedelta(0))),
        fire=Fire(AT, date(2026, 3, 18), "k"),
        run_id="srun_t",
        now=AT,
        settings=settings,
        notifier=None,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize("registry", [LOCAL_ACTIONS, API_ACTIONS, IN_PROCESS_ACTIONS])
def test_the_job_skips_without_open_tickets(tmp_path, registry):
    out = registry.get("live_submit")(_ctx(_settings(tmp_path)))
    assert out.status == "skipped" and out.detail["reason"] == "no_open_tickets"


def test_a_ticket_with_no_broker_is_reported(tmp_path):
    settings = _settings(tmp_path)
    now = datetime.now(UTC)
    with SqliteState(settings.state.path) as state:
        write_tickets(
            state,
            [Order(client_id="c1", ticker="A.US", side="buy", quantity=1.0)],
            portfolio_id="pf_default",
            tick_id=None,
            as_of=date(2026, 3, 17),
            window=SubmitWindow(now - timedelta(minutes=5), now + timedelta(hours=1)),
            hold=lambda _: None,
            now=now,
        )
    out = LOCAL_ACTIONS.get("live_submit")(_ctx(settings))
    assert out.status == "failed"
    assert "does not trade at a broker" in out.detail["errors"]["pf_default"]
    with SqliteState(settings.state.path) as state:
        assert list_tickets(state)[0].status == "approved"  # left to expire
