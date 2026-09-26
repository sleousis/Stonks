"""Reading a new password on the command line: from ``STONKS_AUTH_PASSWORD``
(for scripts) or prompted twice without echo. Never from argv, so it can't
land in shell history or the process list."""

from __future__ import annotations

import getpass
import os

from stonks.app.errors import AppError

PASSWORD_ENV = "STONKS_AUTH_PASSWORD"


def read_new_password() -> str:
    from_env = os.environ.get(PASSWORD_ENV)
    if from_env:
        return from_env
    first = getpass.getpass("New password: ")
    if getpass.getpass("Repeat it: ") != first:
        raise AppError("the passwords differ")
    return first
