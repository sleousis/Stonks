"""Backup retention (grandfather-father-son) selection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from stonks.ops.backup import BackupRef, expired_backups
from stonks.ops.config import BackupRetention


def _refs(times):
    return [BackupRef(id=f"b{i}", created_at=t) for i, t in enumerate(times)]


def _kept(refs, policy):
    gone = {r.id for r in expired_backups(refs, policy)}
    return [r.id for r in refs if r.id not in gone]


def test_daily_keeps_newest_per_day():
    base = datetime(2026, 9, 20, 1, tzinfo=UTC)
    times = [base + timedelta(days=d, hours=h) for d in range(5) for h in (0, 12)]
    refs = _refs(times)
    kept = _kept(refs, BackupRetention(daily=3, weekly=0, monthly=0))
    # Newest of each of the last three days: indices 9, 7, 5.
    assert sorted(kept) == sorted(["b9", "b7", "b5"])


def test_weekly_and_monthly_reach_further_back():
    base = datetime(2026, 1, 1, tzinfo=UTC)
    times = [base + timedelta(days=d) for d in range(0, 120)]
    refs = _refs(times)
    kept = set(_kept(refs, BackupRetention(daily=2, weekly=2, monthly=3)))
    newest = refs[-1]
    assert newest.id in kept
    months = {r.created_at.strftime("%Y-%m") for r in refs if r.id in kept}
    assert len(months) == 3
    assert len(kept) <= 2 + 2 + 3


def test_newest_always_kept_even_with_zero_policy():
    refs = _refs([datetime(2026, 1, d, tzinfo=UTC) for d in range(1, 4)])
    assert _kept(refs, BackupRetention(daily=0, weekly=0, monthly=0)) == ["b2"]


def test_order_of_input_does_not_matter():
    base = datetime(2026, 3, 1, tzinfo=UTC)
    refs = _refs([base + timedelta(days=d) for d in range(10)])
    policy = BackupRetention(daily=3, weekly=0, monthly=0)
    assert sorted(_kept(refs, policy)) == sorted(_kept(list(reversed(refs)), policy))


def test_empty():
    assert expired_backups([], BackupRetention()) == []
