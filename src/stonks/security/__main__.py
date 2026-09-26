"""``python -m stonks.security keygen [--id ID]``: print a new master key
entry for ``STONKS_SECRET_KEYS`` (put the new entry first to make it active,
keep the old ones after it until every token is rotated)."""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime

from stonks.security.crypto import KEYS_ENV, generate_key


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m stonks.security")
    sub = parser.add_subparsers(dest="command", required=True)
    keygen = sub.add_parser("keygen", help="print a new master key entry")
    keygen.add_argument("--id", default=datetime.now(UTC).strftime("k%Y%m%d"))
    args = parser.parse_args(argv)
    if args.command == "keygen":
        print(f"{args.id}:{generate_key()}")
        print(
            f"# add it in front of {KEYS_ENV} (comma-separated); keep old keys until rotated",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
