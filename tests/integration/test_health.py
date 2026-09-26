"""Operational health checks (roadmap 2.5b)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from stonks.config import HealthConfig
from stonks.notify import Notification, Notifier
from stonks.production.health import check_health, notify_unhealthy
from stonks.store.state import SqliteState

# lake_trending's last bar is 2026-04-01 (a Wednesday).
NOW = datetime(2026, 4, 3, 12, 0, tzinfo=UTC)
UNIVERSE = ["UP.US", "FLAT.US"]


class Recorder(Notifier):
    def __init__(self) -> None:
        self.sent: list[Notification] = []

    def _send(self, notification: Notification) -> None:
        self.sent.append(notification)


@pytest.fixture
def state(tmp_path):
    s = SqliteState(tmp_path / "state.sqlite")
    s.migrate()
    yield s
    s.close()


def _by_name(report):
    return {c.name: c for c in report.checks}


def test_all_healthy(state, lake_trending):
    report = check_health(state, lake_trending, UNIVERSE, HealthConfig(), now=NOW)
    assert report.healthy, report.failures
    names = _by_name(report)
    assert names["freshness:UP.US"].ok
    assert names["stuck_ticks"].ok
    assert names["stuck_ingest_runs"].ok
    assert names["ingest_failures"].ok


def test_stale_bars_are_unhealthy(state, lake_trending):
    later = NOW + timedelta(days=10)
    report = check_health(state, lake_trending, UNIVERSE, HealthConfig(), now=later)
    assert not report.healthy
    check = _by_name(report)["freshness:UP.US"]
    assert not check.ok
    assert "2026-04-01" in check.detail


def test_ticker_without_bars_is_unhealthy(state, lake_trending):
    report = check_health(state, lake_trending, ["NOPE.US"], HealthConfig(), now=NOW)
    check = _by_name(report)["freshness:NOPE.US"]
    assert not check.ok
    assert "no daily bars" in check.detail


def test_freshness_threshold_is_configurable(state, lake_trending):
    report = check_health(state, lake_trending, UNIVERSE, HealthConfig(max_bar_age_days=1), now=NOW)
    assert not _by_name(report)["freshness:UP.US"].ok


def test_tick_stuck_in_running_is_unhealthy(state, lake_trending):
    old = (NOW - timedelta(minutes=90)).isoformat(timespec="seconds")
    recent = (NOW - timedelta(minutes=5)).isoformat(timespec="seconds")
    state.execute(
        "INSERT INTO tick_runs (id, started_at, status) VALUES ('t_old', ?, 'running')", [old]
    )
    state.execute(
        "INSERT INTO tick_runs (id, started_at, status) VALUES ('t_new', ?, 'running')", [recent]
    )
    report = check_health(state, lake_trending, UNIVERSE, HealthConfig(), now=NOW)
    check = _by_name(report)["stuck_ticks"]
    assert not check.ok
    assert "t_old" in check.detail
    assert "t_new" not in check.detail


def test_ingest_run_stuck_in_running_is_unhealthy(state, lake_trending):
    run_id = lake_trending.open_ingest_run("eodhd", "prices")
    lake_trending.sql(
        "UPDATE ingest_runs SET started_at = ? WHERE id = ?",
        [NOW - timedelta(hours=5), run_id],
    )
    report = check_health(state, lake_trending, UNIVERSE, HealthConfig(), now=NOW)
    check = _by_name(report)["stuck_ingest_runs"]
    assert not check.ok
    assert str(run_id) in check.detail


def test_recent_ingest_failure_is_unhealthy_old_one_is_not(state, lake_trending):
    recent = lake_trending.open_ingest_run("eodhd", "prices")
    lake_trending.close_ingest_run(recent, 0, 2, "error", "boom")
    lake_trending.sql(
        "UPDATE ingest_runs SET started_at = ? WHERE id = ?", [NOW - timedelta(hours=2), recent]
    )
    old = lake_trending.open_ingest_run("eodhd", "prices")
    lake_trending.close_ingest_run(old, 0, 2, "error", "old boom")
    lake_trending.sql(
        "UPDATE ingest_runs SET started_at = ? WHERE id = ?", [NOW - timedelta(days=3), old]
    )
    report = check_health(state, lake_trending, UNIVERSE, HealthConfig(), now=NOW)
    check = _by_name(report)["ingest_failures"]
    assert not check.ok
    assert f"#{recent}" in check.detail
    assert f"#{old}" not in check.detail


def test_crashing_check_is_reported_unhealthy_not_raised(tmp_path, lake_trending):
    unmigrated = SqliteState(tmp_path / "empty.sqlite")
    try:
        report = check_health(unmigrated, lake_trending, UNIVERSE, HealthConfig(), now=NOW)
    finally:
        unmigrated.close()
    check = _by_name(report)["stuck_ticks"]
    assert not check.ok
    assert "error" in check.detail


def test_notify_unhealthy_sends_error_with_failures(state, lake_trending):
    later = NOW + timedelta(days=10)
    report = check_health(state, lake_trending, UNIVERSE, HealthConfig(), now=later)
    rec = Recorder()
    notify_unhealthy(report, rec)
    [n] = rec.sent
    assert n.level == "error"
    assert "freshness:UP.US" in n.fields["failed_checks"]


def test_notify_unhealthy_is_silent_when_healthy(state, lake_trending):
    report = check_health(state, lake_trending, UNIVERSE, HealthConfig(), now=NOW)
    rec = Recorder()
    notify_unhealthy(report, rec)
    assert rec.sent == []


def test_health_with_halts_opens_the_operational_halt_and_reports_it(state, lake_trending):
    from stonks.production.halts import active_halts, run_health

    later = NOW + timedelta(days=10)
    report = run_health(state, lake_trending, UNIVERSE, HealthConfig(), now=later)
    names = _by_name(report)
    assert not names["freshness:UP.US"].ok
    assert not names["risk_halts"].ok and "operational" in names["risk_halts"].detail
    [halt] = active_halts(state, later.date())
    assert (halt.kind, halt.scope) == ("operational", "global")

    healthy = run_health(state, lake_trending, UNIVERSE, HealthConfig(), now=NOW)
    assert healthy.healthy, healthy.failures
    assert _by_name(healthy)["risk_halts"].ok
