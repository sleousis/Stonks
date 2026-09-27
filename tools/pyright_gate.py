"""Fail CI only on new pyright errors (TT-02).

Pyright runs in basic mode over ``src/stonks`` (``[tool.pyright]`` in
``pyproject.toml``). The errors that existed when the gate was added are
counted per file and rule in ``tools/pyright-baseline.json``. A run fails
when any file has more errors of a rule than the baseline allows. Line
numbers are not part of the key, so moving code does not trip the gate.

    uv run python -m tools.pyright_gate            # run pyright and compare
    uv run python -m tools.pyright_gate --update   # accept the current errors

Run ``--update`` after fixing errors so the baseline only shrinks.

Packages in ``strict`` under ``[tool.pyright]`` run in strict mode and may
carry no errors at all: an error there fails the gate even when the
baseline lists it, and ``--update`` refuses to baseline it (BL-49).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "tools" / "pyright-baseline.json"


def error_counts(report: dict[str, Any], root: Path = ROOT) -> dict[str, int]:
    """Errors per ``<repo-relative file>::<rule>``."""
    counts: dict[str, int] = {}
    for diag in report.get("generalDiagnostics", []):
        if diag.get("severity") != "error":
            continue
        path = Path(diag["file"])
        try:
            rel = path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            rel = path.as_posix()
        key = f"{rel}::{diag.get('rule', 'error')}"
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def strict_paths(root: Path = ROOT) -> list[str]:
    """The ``strict`` entries of ``[tool.pyright]`` in ``pyproject.toml``."""
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    return [str(p).rstrip("/") for p in data.get("tool", {}).get("pyright", {}).get("strict", [])]


def strict_errors(counts: dict[str, int], strict: list[str]) -> dict[str, int]:
    """Errors in files under a strict path (these are never baselined)."""
    return {
        k: n
        for k, n in counts.items()
        if any(k.split("::", 1)[0].startswith(p + "/") for p in strict)
    }


def compare(
    counts: dict[str, int], baseline: dict[str, int]
) -> tuple[dict[str, int], dict[str, int]]:
    """``(new, fixed)``: errors above the baseline, and baseline errors gone."""
    new = {k: n - baseline.get(k, 0) for k, n in counts.items() if n > baseline.get(k, 0)}
    fixed = {k: n - counts.get(k, 0) for k, n in baseline.items() if counts.get(k, 0) < n}
    return new, fixed


def _run_pyright() -> dict[str, Any]:
    proc = subprocess.run(
        [sys.executable, "-m", "pyright", "--outputjson"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        sys.stderr.write(proc.stdout + proc.stderr)
        raise SystemExit(2) from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--baseline", type=Path, default=BASELINE)
    parser.add_argument("--report", type=Path, help="pyright --outputjson file (default: run it)")
    parser.add_argument("--update", action="store_true", help="write the current errors")
    parser.add_argument(
        "--strict", action="append", help="a strict path (default: [tool.pyright] strict)"
    )
    args = parser.parse_args(argv)

    report = json.loads(args.report.read_text()) if args.report else _run_pyright()
    counts = error_counts(report)
    strict = strict_errors(counts, args.strict if args.strict else strict_paths())
    if strict:
        print("pyright errors under a strict path (never baselined; fix them):")
        for key, n in strict.items():
            print(f"  {key} {n}")
        return 1
    if args.update:
        args.baseline.write_text(json.dumps(counts, indent=2, sort_keys=True) + "\n")
        print(f"pyright baseline: {sum(counts.values())} error(s) in {args.baseline.name}")
        return 0

    baseline = json.loads(args.baseline.read_text()) if args.baseline.exists() else {}
    new, fixed = compare(counts, baseline)
    total = sum(counts.values())
    print(f"pyright: {total} error(s), baseline {sum(baseline.values())}")
    if fixed:
        print(f"{sum(fixed.values())} baseline error(s) fixed; run with --update to lock it in")
    if not new:
        return 0
    print("New pyright errors (file::rule +count):")
    for key, n in new.items():
        print(f"  {key} +{n}")
    files = {k.split("::", 1)[0] for k in new}
    for diag in report.get("generalDiagnostics", []):
        rel = error_counts({"generalDiagnostics": [diag]})
        key = next(iter(rel), "")
        if key in new and key.split("::", 1)[0] in files:
            line = diag["range"]["start"]["line"] + 1
            print(f"  {key.split('::', 1)[0]}:{line}: {diag['message'].splitlines()[0]}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
