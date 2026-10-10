"""Print the CSP hashes of an index.html's inline scripts.

    uv run python -m tools.csp_hashes [path]   # default: web/src/index.html

Copy them into ``CONSOLE_SCRIPT_HASHES`` (src/stonks/api/csp.py) and the
``Content-Security-Policy`` line of deploy/Caddyfile after changing an
inline script of the console.
"""

from __future__ import annotations

import sys
from pathlib import Path

from stonks.api.csp import CONSOLE_SCRIPT_HASHES, console_policy, inline_script_hashes


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    path = Path(args[0]) if args else Path("web/src/index.html")
    found = inline_script_hashes(path.read_bytes().decode("utf-8"))
    for digest in found:
        print(digest)
    if tuple(found) != CONSOLE_SCRIPT_HASHES:
        print("The policy is out of date. Put these in CONSOLE_SCRIPT_HASHES,", file=sys.stderr)
        print("then set this line in deploy/Caddyfile:", file=sys.stderr)
        print(f'  ?Content-Security-Policy "{console_policy()}"', file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
