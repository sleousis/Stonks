"""``python -m stonks.auth``: operator entry points for sign-in.

Shell access to the server already implies admin, so these run without a
login. The password is read from ``STONKS_AUTH_PASSWORD`` when set (for
scripts), otherwise prompted twice without echo. It is never an argument,
so it can't land in shell history.

python -m stonks.auth bootstrap-admin --email you@example.com [--state PATH]
    Give the bootstrap admin (usr_owner) an email and a password so the
    first login works. The second factor is set up at that login.

python -m stonks.auth reset-password --email you@example.com [--state PATH]
    Set a new password for any user and sign them out everywhere.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from stonks.app.errors import AppError
from stonks.auth.passwords import PasswordHasher
from stonks.auth.service import AuthService
from stonks.store.state import SqliteState

PASSWORD_ENV = "STONKS_AUTH_PASSWORD"


def _hasher() -> PasswordHasher:
    return PasswordHasher()


def _state_path(arg: str | None) -> Path:
    if arg:
        return Path(arg)
    from stonks.config import load_settings

    return Path(load_settings().state.path)


def _password() -> str:
    from_env = os.environ.get(PASSWORD_ENV)
    if from_env:
        return from_env
    first = getpass.getpass("New password: ")
    if getpass.getpass("Repeat it: ") != first:
        raise AppError("the passwords differ")
    return first


def _service(path: Path) -> AuthService:
    @contextmanager
    def factory() -> Iterator[SqliteState]:
        with SqliteState(path) as state:
            yield state

    return AuthService(factory, hasher=_hasher())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m stonks.auth")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("bootstrap-admin", "set the first admin's email and password"),
        ("reset-password", "set a new password for a user"),
    ):
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument("--email", required=True)
        cmd.add_argument("--state", help="state.sqlite path (default: from config)")
    args = parser.parse_args(argv)
    path = _state_path(args.state)
    try:
        svc = _service(path)
        if args.command == "bootstrap-admin":
            user = svc.bootstrap_admin(args.email, _password())
            print(f"bootstrap admin {user.id} can now sign in as {user.email}")
        else:
            user = svc.set_password_by_email(args.email, _password())
            print(f"password reset for {user.id}; their sessions were signed out")
    except AppError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
