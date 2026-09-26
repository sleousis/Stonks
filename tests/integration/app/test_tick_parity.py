"""A tick started from the API runs with exactly what `stonks tick` runs with."""

from __future__ import annotations

from datetime import date

import pytest
from typer.testing import CliRunner

import stonks.app.ticks as app_ticks
import stonks.cli as cli
from stonks.app.context import AppContext
from stonks.app.errors import ConflictError
from stonks.app.services import Services
from stonks.app.ticks import TickRequest
from stonks.production.tick import BackdatedTickError, TickResult


@pytest.fixture
def configured(settings, seeded):
    return settings.model_copy(
        update={
            "production": settings.production.model_copy(
                update={
                    "universe": ["UP.US"],
                    "threshold": 0.01,
                    "max_price_staleness_days": 3,
                    "shadow_enabled": False,
                    "risk": settings.production.risk.model_copy(
                        update={"max_open_positions": 1, "cash_buffer_fraction": 0.1}
                    ),
                }
            ),
            "notify": settings.notify.model_copy(update={"min_level": "info"}),
        }
    )


def _spy(monkeypatch, module, calls):
    def fake_run_tick(**kwargs):
        calls.append(kwargs)
        return TickResult(
            tick_id="tick_x", status="noop", winner_strategy_id=None, orders_placed=0, fills=0
        )

    monkeypatch.setattr(module, "run_tick", fake_run_tick)


@pytest.fixture
def services_for(fake_source):
    created = []

    def make(settings):
        svc = Services.create(AppContext(settings, source_factory=lambda: fake_source))
        svc.start()
        created.append(svc)
        return svc

    yield make
    for svc in created:
        svc.shutdown()


def test_api_and_cli_build_identical_tick_settings_and_notifier(
    configured, services_for, monkeypatch
):
    cli_calls: list[dict] = []
    api_calls: list[dict] = []
    _spy(monkeypatch, cli, cli_calls)
    _spy(monkeypatch, app_ticks, api_calls)
    monkeypatch.setattr(cli, "_settings", lambda: configured)

    result = CliRunner().invoke(cli.app, ["tick", "--as-of", "2026-03-25"])
    assert result.exit_code == 0, result.output

    services_for(configured).ticks.run(TickRequest(as_of=date(2026, 3, 25)))

    [c], [a] = cli_calls, api_calls
    assert a["settings"] == c["settings"]
    assert a["settings"].risk.max_open_positions == 1
    assert a["settings"].shadow_enabled is False
    assert a["settings"].max_price_staleness_days == 3
    for call in (a, c):
        assert call["notifier"] is not None
        assert call["notifier"].min_level == "info"
    assert type(a["notifier"]) is type(c["notifier"])
    assert a.get("broker_factory") is None
    assert c.get("broker_factory") is None


def test_api_backdated_tick_is_a_conflict(configured, services_for, monkeypatch):
    def refuse(**kwargs):
        raise BackdatedTickError("refusing to trade as_of 2026-03-19")

    monkeypatch.setattr(app_ticks, "run_tick", refuse)
    with pytest.raises(ConflictError, match="2026-03-19"):
        services_for(configured).ticks.run(TickRequest(as_of=date(2026, 3, 19)))
