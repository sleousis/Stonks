"""The notification ``DeliveryWorker`` hosted next to the scheduler.

The router queues notifications in the state DB's outbox; the worker sends
them outside any producer's transaction. It runs on its own daemon thread
in whichever process runs the scheduler (``stonks schedule run`` or the
in-process scheduler of ``stonks serve``), with its own SQLite connection
built on that thread. Off with ``[scheduler].deliver_notifications = false``.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from stonks.logging import get_logger
from stonks.notify.channels import build_channels
from stonks.notify.settings import NotifySettings
from stonks.notify.worker import DeliveryWorker
from stonks.scheduling.config import scheduler_config_from
from stonks.store.state import SqliteState

_log = get_logger("stonks.scheduling.delivery")

THREAD_NAME = "stonks-notify-delivery"


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
