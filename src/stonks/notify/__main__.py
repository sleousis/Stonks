"""``python -m stonks.notify``: operator entry points for notifications.

python -m stonks.notify vapid-keygen
    Print a new VAPID key pair as env lines. Put both in the deployment's
    secrets (the private key is a credential), set STONKS_VAPID_SUBJECT
    to a mailto: or https: contact, and restart. Rotating the pair
    invalidates every browser subscription (users re-enable push).

python -m stonks.notify test --user <email-or-id> [--urgent] [--state PATH]
    Queue a test notification for one user and run one delivery pass.

python -m stonks.notify deliver [--state PATH]
    Run one delivery pass (the worker normally runs in the scheduler).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from stonks.accounts import NotFound, UserRepository
from stonks.notify.channels import build_channels
from stonks.notify.events import Audience, Event
from stonks.notify.router import NotificationRouter
from stonks.notify.settings import NotifySettings
from stonks.notify.webpush import generate_vapid_keys
from stonks.notify.worker import DeliveryWorker, WorkerStats
from stonks.store.state import SqliteState


def _state_path(arg: str | None) -> Path:
    if arg:
        return Path(arg)
    from stonks.config import load_settings

    return Path(load_settings().state.path)


def _print_stats(stats: WorkerStats) -> None:
    print(
        f"sent={stats.sent} retried={stats.retried} dead={stats.dead}"
        f" deferred={stats.deferred} skipped={stats.skipped}"
    )


def _keygen() -> int:
    keys = generate_vapid_keys()
    print(f"STONKS_VAPID_PUBLIC_KEY={keys.public_key}")
    print(f"STONKS_VAPID_PRIVATE_KEY={keys.private_key}")
    print(
        "# Store both as secrets (never commit the private key); also set"
        " STONKS_VAPID_SUBJECT=mailto:you@example.com",
        file=sys.stderr,
    )
    return 0


def _test(args: argparse.Namespace) -> int:
    settings = NotifySettings.from_env()
    channels = build_channels(settings)
    with SqliteState(_state_path(args.state)) as state:
        state.migrate()
        users = UserRepository(state)
        try:
            user = users.get_by_email(args.user) if "@" in args.user else users.get(args.user)
        except NotFound:
            print("user not found", file=sys.stderr)
            return 2
        router = NotificationRouter(state, channels, settings.outbox, secrets=settings.secrets)
        result = router.publish(
            Event(
                category="system",
                title="Test notification",
                body="If you can read this, notifications reach you.",
                audience=Audience.users(user.id),
                urgency="high" if args.urgent else "normal",
                deep_link="/notifications",
            )
        )
        if not result.notification_ids:
            print("user is not active; nothing queued", file=sys.stderr)
            return 2
        print(
            f"queued notification {result.notification_ids[0]} with"
            f" {result.deliveries} deliveries (channels: {', '.join(sorted(channels)) or 'none'})"
        )
        _print_stats(DeliveryWorker(state, channels, settings.outbox).run_once())
    return 0


def _deliver(args: argparse.Namespace) -> int:
    settings = NotifySettings.from_env()
    with SqliteState(_state_path(args.state)) as state:
        state.migrate()
        worker = DeliveryWorker(state, build_channels(settings), settings.outbox)
        _print_stats(worker.run_once())
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m stonks.notify", description=__doc__.split("\n")[0]
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("vapid-keygen", help="print a new VAPID key pair as env lines")
    test = sub.add_parser("test", help="send a test notification to one user")
    test.add_argument("--user", required=True, help="email or user id")
    test.add_argument("--urgent", action="store_true", help="high urgency (skips quiet hours)")
    test.add_argument("--state", help="state DB path (default: from config)")
    deliver = sub.add_parser("deliver", help="run one delivery pass")
    deliver.add_argument("--state", help="state DB path (default: from config)")
    args = parser.parse_args(argv)
    if args.command == "vapid-keygen":
        return _keygen()
    if args.command == "test":
        return _test(args)
    return _deliver(args)


if __name__ == "__main__":
    import dotenv

    from stonks.logging import configure_logging

    dotenv.load_dotenv()
    configure_logging()
    raise SystemExit(main())
