"""stonks serve delivers alerts by default (docs/without-a-broker.md).

``[notify] deliver_in_serve``: ``auto`` runs the delivery worker in serve
unless a scheduler already delivers (the hosted in-process scheduler, or a
separate ``stonks schedule run`` holding the scheduler lock), ``on``
always runs it, ``off`` never does. Two workers never send a delivery
twice: each claims its rows in one UPDATE with a lease."""

from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from stonks.api import create_app
from stonks.app.context import AppContext
from stonks.app.services import Services
from stonks.config import ApiConfig, LakeConfig, RegistryConfig, Settings, StateConfig
from stonks.notify.events import Audience, Event
from stonks.notify.router import NotificationRouter
from stonks.notify.settings import OutboxSettings
from stonks.notify.worker import DeliveryWorker
from stonks.scheduling import delivery
from stonks.scheduling.config import SchedulerConfig
from stonks.scheduling.scheduler import InstanceLock, default_lock_path

from .fakes import FakePush, add_device


@pytest.fixture(autouse=True)
def _serve_delivery_on(monkeypatch):
    # The suite keeps the serve worker off so API tests see their queued
    # rows; these tests are about it, so they turn it back on.
    monkeypatch.setattr(delivery, "SERVE_DELIVERY_ENABLED", True)


@pytest.fixture
def push(monkeypatch) -> FakePush:
    fake = FakePush()
    monkeypatch.setattr(delivery, "build_channels", lambda _notify: {"webpush": fake})
    return fake


@pytest.fixture
def settings(tmp_path, state) -> Settings:
    out = Settings(
        lake=LakeConfig(path=tmp_path / "lake.duckdb"),
        state=StateConfig(path=tmp_path / "state.sqlite"),
        registry=RegistryConfig(artifacts_dir=tmp_path / "artifacts"),
        api=ApiConfig(ui_dist=tmp_path / "no-dist"),
        scheduler=SchedulerConfig(backend="local", delivery_interval_seconds=0.02),
    )
    out.api.allowed_hosts = ["testserver"]
    return out


@pytest.fixture
def alice(state, users) -> str:
    uid = users["alice"].id
    add_device(state, uid, "psh_1")
    return uid


def _publish(state, push, user_id: str, n: int = 1) -> None:
    router = NotificationRouter(state, {"webpush": push})
    for i in range(n):
        router.publish(
            Event(
                audience=Audience.users(user_id),
                category="order",
                title="Order filled",
                body=f"order {i}",
                dedupe_key=f"order:{i}",
            )
        )


def _sent_rows(state) -> int:
    return state.sql("SELECT COUNT(*) AS n FROM notification_deliveries WHERE status = 'sent'")[0][
        "n"
    ]


def _wait_sent(state, n: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _sent_rows(state) >= n:
            return
        time.sleep(0.02)
    raise AssertionError(f"only {_sent_rows(state)} of {n} deliveries were sent")


def _app(settings: Settings):
    return create_app(settings, services=Services.create(AppContext(settings)))


def test_the_default_is_auto():
    assert Settings().notify.deliver_in_serve == "auto"


@pytest.mark.parametrize(
    ("mode", "hosted_scheduler", "hosted_delivery", "elsewhere", "expected"),
    [
        ("auto", False, False, False, True),  # serve alone
        ("auto", False, False, True, False),  # a separate scheduler delivers
        ("auto", True, True, False, False),  # the hosted scheduler delivers
        ("auto", True, False, False, False),  # the hosted scheduler was told not to
        ("on", False, False, True, True),
        ("on", True, False, False, True),
        ("on", True, True, False, False),  # never two workers in one process
        ("off", False, False, False, False),
    ],
)
def test_who_delivers(mode, hosted_scheduler, hosted_delivery, elsewhere, expected):
    assert (
        delivery.serve_delivers(
            mode,
            hosted_scheduler=hosted_scheduler,
            hosted_delivery=hosted_delivery,
            scheduler_elsewhere=lambda: elsewhere,
        )
        is expected
    )


def test_serve_alone_delivers(settings, state, push, alice):
    app = _app(settings)
    with TestClient(app):
        assert app.state.services.schedule.delivery is not None
        _publish(state, push, alice, n=3)
        _wait_sent(state, 3)
    assert app.state.services.schedule.delivery is None  # stopped with serve
    assert sorted(m.body for m, _ in push.sent) == ["order 0", "order 1", "order 2"]


def test_serve_leaves_delivery_to_a_running_scheduler(settings, state, push, alice):
    lock = InstanceLock(default_lock_path(settings.scheduler, settings.state.path))
    lock.acquire()  # what a separate `stonks schedule run` holds
    try:
        app = _app(settings)
        with TestClient(app):
            assert app.state.services.schedule.delivery is None
    finally:
        lock.release()


def test_serve_and_a_scheduler_never_send_a_delivery_twice(settings, state, push, alice):
    """Even with both workers running (``on``, or a scheduler that started
    after serve), the outbox claim sends each delivery once."""
    settings.notify.deliver_in_serve = "on"
    app = _app(settings)
    stop = threading.Event()
    with TestClient(app):
        assert app.state.services.schedule.delivery is not None
        scheduler_worker = threading.Thread(
            target=lambda: _scheduler_worker(settings, push, stop), daemon=True
        )
        scheduler_worker.start()
        _publish(state, push, alice, n=40)
        _wait_sent(state, 40)
        stop.set()
        scheduler_worker.join(timeout=10)
    bodies = [m.body for m, _ in push.sent]
    assert len(bodies) == 40 and len(set(bodies)) == 40


def _scheduler_worker(settings: Settings, push: FakePush, stop: threading.Event) -> None:
    from stonks.store.state import SqliteState

    with SqliteState(settings.state.path) as own:
        worker = DeliveryWorker(own, {"webpush": push}, OutboxSettings(batch_size=3))
        worker.run_forever(stop, interval_seconds=0.01)


def test_the_setting_turns_it_off(settings, state, push, alice):
    settings.notify.deliver_in_serve = "off"
    app = _app(settings)
    with TestClient(app):
        assert app.state.services.schedule.delivery is None
        _publish(state, push, alice)
        time.sleep(0.2)
    assert push.sent == []
    assert _sent_rows(state) == 0
