"""The notification ``DeliveryWorker`` hosted next to the scheduler.

The router queues notifications in the state DB's outbox; the worker sends
them outside any producer's transaction. It runs on its own daemon thread
in whichever process runs the scheduler (``stonks schedule run`` or the
in-process scheduler of ``stonks serve``), with its own SQLite connection
built on that thread. Off with ``[scheduler].deliver_notifications = false``.

``stonks serve`` runs one too (``[notify] deliver_in_serve``, see
:func:`serve_delivery_plan`), so alerts go out from a lone API process. In
``auto`` it is a standby worker: before each pass it checks the scheduler
lock and sends nothing while a scheduler holds it. So serve steps back when
a scheduler starts after it (the Compose order) and takes over when the
scheduler stops. Two workers never send a delivery twice: each claims its
rows with a lease and its own token (``DeliveryWorker``).
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


#: What serve runs: no worker, one that always delivers, or a standby
#: worker that delivers only while no scheduler holds the lock.
ServePlan = Literal["none", "always", "standby"]


def serve_delivery_plan(
    mode: ServeDelivery, *, hosted_scheduler: bool, hosted_delivery: bool
) -> ServePlan:
    """The delivery worker ``stonks serve`` runs.

    ``off`` none, and never a second worker in a process whose hosted
    scheduler already runs one. ``on`` otherwise always delivers. ``auto``
    runs none next to a hosted scheduler (it follows its own
    ``deliver_notifications``), else a standby worker."""
    if mode == "off" or hosted_delivery:
        return "none"
    if mode == "on":
        return "always"
    return "none" if hosted_scheduler else "standby"


def scheduler_delivers_elsewhere(config: SchedulerConfig, state_path: Any) -> bool:
    """True when another process holds the scheduler lock (a running
    ``stonks schedule run``) and schedulers deliver notifications.

    It takes the lock for an instant to find out. A scheduler starting in
    that instant waits for it (``LOCK_GRACE_SECONDS``) instead of refusing."""
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


class _Standby:
    """The gate of serve's standby worker: deliver only while no scheduler
    delivers. Logs each change, not each pass."""

    def __init__(self, config: SchedulerConfig, state_path: Any) -> None:
        self._config = config
        self._state_path = state_path
        self._active: bool | None = None

    def __call__(self) -> bool:
        try:
            active = not scheduler_delivers_elsewhere(self._config, self._state_path)
        except Exception as exc:  # an unreadable lock file: leave it to the scheduler
            _log.warning("notify.serve_delivery_probe_failed", error_type=type(exc).__name__)
            active = False
        if active != self._active:
            _log.info("notify.serve_delivery_active" if active else "notify.serve_delivery_standby")
            self._active = active
        return active


def start_serve_delivery_worker(
    settings: Any,
    config: SchedulerConfig,
    *,
    hosted_scheduler: bool,
    hosted_delivery: bool,
) -> DeliveryHandle | None:
    """The worker ``stonks serve`` runs, as :func:`serve_delivery_plan` says."""
    mode: ServeDelivery = settings.notify.deliver_in_serve
    plan = serve_delivery_plan(
        mode, hosted_scheduler=hosted_scheduler, hosted_delivery=hosted_delivery
    )
    if not SERVE_DELIVERY_ENABLED or plan == "none":
        _log.info("notify.serve_delivery_skipped", mode=mode)
        return None
    gate = _Standby(config, settings.state.path) if plan == "standby" else None
    return _spawn(settings, config, active=gate)


def _spawn(
    settings: Any, config: SchedulerConfig, *, active: Callable[[], bool] | None = None
) -> DeliveryHandle:
    stop = threading.Event()

    def run() -> None:
        notify = NotifySettings.from_env()
        state = SqliteState(settings.state.path)
        try:
            worker = DeliveryWorker(state, build_channels(notify), notify.outbox)
            worker.run_forever(
                stop, interval_seconds=config.delivery_interval_seconds, active=active
            )
        except Exception as exc:  # pragma: no cover - logged, never kills the host
            _log.error("notify.delivery_worker_failed", error_type=type(exc).__name__)
        finally:
            state.close()

    thread = threading.Thread(target=run, name=THREAD_NAME, daemon=True)
    thread.start()
    _log.info("notify.delivery_worker_started")
    return DeliveryHandle(thread, stop)
