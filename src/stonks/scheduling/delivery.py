"""The notification ``DeliveryWorker`` hosted next to the scheduler.

The router queues notifications in the state DB's outbox; the worker sends
them outside any producer's transaction. It runs on its own daemon thread
in whichever process runs the scheduler (``stonks schedule run`` or the
in-process scheduler of ``stonks serve``), with its own SQLite connection
built on that thread. Off with ``[scheduler].deliver_notifications = false``.

``stonks serve`` runs one too when no scheduler delivers
(``[notify] deliver_in_serve``, see :func:`serve_delivers`), so alerts go
out from a lone API process. Two workers never send a delivery twice: each
claims its rows in one UPDATE with a lease (``DeliveryWorker``).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from stonks.logging import get_logger
from stonks.notify.channels import build_channels
from stonks.notify.settings import NotifySettings
from stonks.notify.worker import DeliveryWorker
from stonks.scheduling.config import SchedulerConfig, scheduler_config_from
from stonks.store.state import SqliteState

_log = get_logger("stonks.scheduling.delivery")

THREAD_NAME = "stonks-notify-delivery"

#: ``[notify] deliver_in_serve``.
ServeDelivery = Literal["auto", "on", "off"]

#: False keeps serve from starting its own worker whatever the setting
#: says. The test suite sets it so API tests see the rows they queued.
SERVE_DELIVERY_ENABLED = True


@dataclass
class DeliveryHandle:
    thread: threading.Thread
    stop_event: threading.Event

    def stop(self, timeout: float = 30.0) -> None:
        self.stop_event.set()
        self.thread.join(timeout=timeout)


def start_delivery_worker(settings: Any) -> DeliveryHandle | None:
    """Start the worker thread, or return ``None`` when switched off."""
    config = scheduler_config_from(settings)
    if not config.deliver_notifications:
        return None
    return _spawn(settings, config)


def serve_delivers(
    mode: ServeDelivery,
    *,
    hosted_scheduler: bool,
    hosted_delivery: bool,
    scheduler_elsewhere: Callable[[], bool],
) -> bool:
    """Whether ``stonks serve`` starts its own delivery worker.

    ``off`` never, and never a second worker in a process whose hosted
    scheduler already runs one. ``on`` otherwise always. ``auto`` only
    when no scheduler delivers: none is hosted here (a hosted one follows
    its own ``deliver_notifications``) and no other process holds the
    scheduler lock with delivery on."""
    if mode == "off" or hosted_delivery:
        return False
    if mode == "on":
        return True
    return not hosted_scheduler and not scheduler_elsewhere()


def scheduler_delivers_elsewhere(config: SchedulerConfig, state_path: Any) -> bool:
    """True when another process holds the scheduler lock (a running
    ``stonks schedule run``) and schedulers deliver notifications."""
    from stonks.scheduling.scheduler import (
        InstanceLock,
        SchedulerAlreadyRunningError,
        default_lock_path,
    )

    if not config.deliver_notifications:
        return False
    lock = InstanceLock(default_lock_path(config, state_path))
    try:
        lock.acquire()
    except SchedulerAlreadyRunningError:
        return True
    lock.release()
    return False


def start_serve_delivery_worker(
    settings: Any,
    config: SchedulerConfig,
    *,
    hosted_scheduler: bool,
    hosted_delivery: bool,
) -> DeliveryHandle | None:
    """The worker ``stonks serve`` runs when :func:`serve_delivers` says so."""
    mode: ServeDelivery = settings.notify.deliver_in_serve
    if not SERVE_DELIVERY_ENABLED or not serve_delivers(
        mode,
        hosted_scheduler=hosted_scheduler,
        hosted_delivery=hosted_delivery,
        scheduler_elsewhere=lambda: scheduler_delivers_elsewhere(config, settings.state.path),
    ):
        _log.info("notify.serve_delivery_skipped", mode=mode)
        return None
    return _spawn(settings, config)


def _spawn(settings: Any, config: SchedulerConfig) -> DeliveryHandle:
    stop = threading.Event()

    def run() -> None:
        notify = NotifySettings.from_env()
        state = SqliteState(settings.state.path)
        try:
            worker = DeliveryWorker(state, build_channels(notify), notify.outbox)
            worker.run_forever(stop, interval_seconds=config.delivery_interval_seconds)
        except Exception as exc:  # pragma: no cover - logged, never kills the host
            _log.error("notify.delivery_worker_failed", error_type=type(exc).__name__)
        finally:
            state.close()

    thread = threading.Thread(target=run, name=THREAD_NAME, daemon=True)
    thread.start()
    _log.info("notify.delivery_worker_started")
    return DeliveryHandle(thread, stop)
