"""run_tick notifies on error / partial status and on rejected orders."""

from __future__ import annotations

from datetime import date

import pytest

import stonks.production.tick as tick_mod
from stonks.notify import Notification, Notifier
from stonks.production.tick import TickSettings, run_tick

AS_OF = date(2026, 3, 20)


class Recorder(Notifier):
    def __init__(self) -> None:
        self.sent: list[Notification] = []

    def _send(self, notification: Notification) -> None:
        self.sent.append(notification)


class RaisingNotifier:
    """Violates the never-raise contract; run_tick must still be safe."""

    def notify(self, notification):
        raise RuntimeError("notifier exploded")


SETTINGS = TickSettings(universe=["UP.US"], initial_cash=10_000.0)


class _RejectingBroker:
    def place_order(self, order):
        return None


class _FailingBroker:
    def place_order(self, order):
        raise RuntimeError("broker down")


def test_ok_tick_sends_nothing(tick_env):
    lake, state, registry = tick_env
    rec = Recorder()
    result = run_tick(state, lake, registry, SETTINGS, as_of=AS_OF, notifier=rec)
    assert result.status == "ok"
    assert rec.sent == []


def test_rejected_orders_notify_warning(tick_env, monkeypatch):
    lake, state, registry = tick_env
    monkeypatch.setattr(tick_mod, "_build_broker", lambda *a, **k: _RejectingBroker())
    rec = Recorder()
    result = run_tick(state, lake, registry, SETTINGS, as_of=AS_OF, notifier=rec)
    [n] = rec.sent
    assert n.level == "warning"
    assert n.fields["tick_id"] == result.tick_id
    assert n.fields["rejected"] == ["UP.US"]


def test_partial_tick_notifies_warning(tick_env, monkeypatch):
    lake, state, registry = tick_env
    monkeypatch.setattr(tick_mod, "_build_broker", lambda *a, **k: _FailingBroker())
    rec = Recorder()
    result = run_tick(state, lake, registry, SETTINGS, as_of=AS_OF, notifier=rec)
    assert result.status == "partial"
    assert any(n.level == "warning" and n.fields.get("status") == "partial" for n in rec.sent)


def test_error_tick_notifies_error_and_still_raises(tick_env, monkeypatch):
    lake, state, registry = tick_env

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(tick_mod, "_snapshot_portfolio", boom)
    rec = Recorder()
    with pytest.raises(RuntimeError, match="disk full"):
        run_tick(state, lake, registry, SETTINGS, as_of=AS_OF, notifier=rec)
    [n] = rec.sent
    assert n.level == "error"
    assert "disk full" in n.message
    assert n.fields["status"] == "error"


def test_raising_notifier_does_not_mask_tick_error(tick_env, monkeypatch):
    lake, state, registry = tick_env

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(tick_mod, "_snapshot_portfolio", boom)
    with pytest.raises(RuntimeError, match="disk full"):
        run_tick(state, lake, registry, SETTINGS, as_of=AS_OF, notifier=RaisingNotifier())
    assert state.sql("SELECT status FROM tick_runs")[0]["status"] == "error"


def test_raising_notifier_does_not_fail_partial_tick(tick_env, monkeypatch):
    lake, state, registry = tick_env
    monkeypatch.setattr(tick_mod, "_build_broker", lambda *a, **k: _FailingBroker())
    result = run_tick(state, lake, registry, SETTINGS, as_of=AS_OF, notifier=RaisingNotifier())
    assert result.status == "partial"
