"""``python -m stonks.scheduling`` entry point, in-process."""

from __future__ import annotations

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
