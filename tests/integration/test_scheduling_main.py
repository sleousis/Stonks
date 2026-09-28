"""``python -m stonks.scheduling`` entry point, in-process."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from stonks.scheduling.__main__ import EXIT_ALREADY_RUNNING, EXIT_USAGE, main
from stonks.scheduling.jobs import JobOutcome
from stonks.scheduling.local import register_action
from stonks.scheduling.scheduler import InstanceLock


@register_action("cli_test")
def _cli_test(ctx):
    return JobOutcome("succeeded", {"as_of": ctx.fire.as_of.isoformat()})


@pytest.fixture
def config(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("STONKS_DATA_DIR", str(tmp_path / "data"))
    path = tmp_path / "c.toml"
    path.write_text(
        '[scheduler]\n\n[[scheduler.jobs]]\nname = "hourly"\naction = "cli_test"\n'
        'trigger = { type = "interval", every_minutes = 60 }\n',
        encoding="utf-8",
    )
    return path


def test_next_lists_jobs(config, capsys):
    assert main(["--config", str(config), "next"]) == 0
    out = capsys.readouterr().out
    assert "hourly" in out and "every 60 min" in out


def test_run_now_then_runs(config, capsys):
    assert main(["--config", str(config), "run-now", "hourly", "--as-of", "2026-09-25"]) == 0
    assert "succeeded" in capsys.readouterr().out
    assert main(["--config", str(config), "runs"]) == 0
    assert "manual:" in capsys.readouterr().out
    assert main(["--config", str(config), "run-now", "nope"]) == EXIT_USAGE


def test_run_refuses_a_second_instance(config, tmp_path, capsys):
    with InstanceLock(tmp_path / "data" / "scheduler.lock"):
        assert main(["--config", str(config), "run"]) == EXIT_ALREADY_RUNNING
    assert "another scheduler" in capsys.readouterr().err


def test_disabled_scheduler_does_not_run(config, capsys):
    config.write_text("[scheduler]\nenabled = false\n", encoding="utf-8")
    assert main(["--config", str(config), "run"]) == EXIT_USAGE


def test_metrics_and_check(config, capsys):
    assert main(["--config", str(config), "metrics"]) == 0
    out = capsys.readouterr().out
    assert "# TYPE stonks_orders_total counter" in out
    assert "stonks_scheduled_job_next_run_timestamp_seconds" in out
    assert main(["--config", str(config), "check"]) == 0


seen_universes: list[object] = []


@register_action("cli_universe")
def _cli_universe(ctx):
    seen_universes.append(ctx.settings.production.universe)
    return JobOutcome("succeeded")


def test_a_running_scheduler_uses_console_overrides_written_after_it_started(config):
    """``next_run`` overrides (risk, universe) apply from the next job
    without a restart (docs/operations.md), also in a scheduler process."""
    from stonks.config_overrides import OverrideStore
    from stonks.notify import Notifier
    from stonks.scheduling.__main__ import _load, _scheduler, _store
    from stonks.scheduling.triggers import Fire
    from stonks.store.state import SqliteState

    class Quiet(Notifier):
        def _send(self, n):
            return None

    config.write_text(
        '[scheduler]\n\n[[scheduler.jobs]]\nname = "u"\naction = "cli_universe"\n'
        'trigger = { type = "interval", every_minutes = 60 }\n',
        encoding="utf-8",
    )
    seen_universes.clear()
    ld = _load(config)
    store = _store(ld.settings)
    scheduler = _scheduler(ld, store, Quiet())
    with SqliteState(ld.settings.state.path) as state:
        OverrideStore(state).set(
            "production.universe", ["NEW.US"], actor="user:ada", reason="new universe"
        )
    now = datetime.now(UTC)
    scheduler.run_one(ld.specs[0], Fire(now, now.date(), "k1"))
    assert seen_universes == [["NEW.US"]]
