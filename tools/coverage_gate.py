"""Per-package coverage gate (TT-01).

The Phase 18 gate is coverage.py's combined line-and-branch number: at
least 90 overall (``fail_under`` in ``[tool.coverage.report]``) and at least
95 in the five strict packages below. Where a package is still under its
target, its floor is the measured value rounded down, so CI passes today
and the floor only ever moves up. Raise a floor when coverage improves.

    uv run pytest -n auto --cov --cov-report=json
    uv run python -m tools.coverage_gate coverage.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

#: The target for every strict package.
TARGET = 95.0

#: Floors enforced now. Measured on 2026-09-27 (combined line and branch):
#: core 98.4, production 95.5, execution 95.9, auth 98.5, portfolio 96.3.
PACKAGE_FLOORS: dict[str, float] = {
    "core": 95.0,
    "production": 95.0,
    "execution": 95.0,
    "auth": 95.0,
    "portfolio": 95.0,
}


def _in_package(path: str, package: str) -> bool:
    return f"stonks/{package}/" in path.replace("\\", "/")


def package_percent(data: dict[str, Any], package: str) -> float | None:
    """Combined line-and-branch percent of one ``stonks.<package>``, the
    way coverage.py computes its total: covered lines plus covered branches
    over all lines plus all branches."""
    total = covered = 0
    for path, entry in data.get("files", {}).items():
        if not _in_package(path, package):
            continue
        s = entry["summary"]
        units = s["num_statements"] + s.get("num_branches", 0)
        total += units
        covered += units - s["missing_lines"] - s.get("missing_branches", 0)
    if total == 0:
        return None
    return 100.0 * covered / total


def check(
    data: dict[str, Any], floors: dict[str, float] = PACKAGE_FLOORS
) -> list[tuple[str, float | None, float]]:
    """``(package, measured, floor)`` for every package under its floor."""
    failures = []
    for package, floor in floors.items():
        got = package_percent(data, package)
        if got is None or got < floor:
            failures.append((package, got, floor))
    return failures


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    path = Path(args[0] if args else "coverage.json")
    data = json.loads(path.read_text())
    for package, floor in PACKAGE_FLOORS.items():
        got = package_percent(data, package)
        shown = "no files" if got is None else f"{got:.1f}"
        print(f"{package:<12} {shown:>8}  floor {floor:.0f}  target {TARGET:.0f}")
    failures = check(data)
    for package, got, floor in failures:
        shown = "no files" if got is None else f"{got:.2f}"
        print(f"FAIL {package}: {shown} is below the floor of {floor:.0f}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
