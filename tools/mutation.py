"""Mutation testing of the money paths with cosmic-ray (TT-04).

Each target is one module and the tests that guard it. For every target
the runner copies ``src/``, ``tests/`` and ``pyproject.toml`` into a temp
folder, points ``PYTHONPATH`` at the copy, and lets cosmic-ray mutate the
copy. The working tree is never touched, so it is safe to run while you
work. It prints one line per target and writes a JSON summary.

    uv run --with cosmic-ray python -m tools.mutation                  # all targets
    uv run --with cosmic-ray python -m tools.mutation --only pnl,fills
    uv run --with cosmic-ray python -m tools.mutation --summary out.json

The gate (docs/roadmap.md, Phase 18) is a surviving-mutant rate under 10
percent. Runs are slow (every mutant runs the target's tests), so CI runs
this weekly and on demand (.github/workflows/mutation.yml), not per PR.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

#: The gate: surviving mutants per killed-or-survived mutant, in percent.
GATE_PERCENT = 10.0


@dataclass(frozen=True)
class Target:
    name: str
    module: str
    tests: tuple[str, ...]
    timeout: float = 120.0


#: Money paths: orders, fills, risk rules, the ledger sync and P&L.
TARGETS: tuple[Target, ...] = (
    Target("pnl", "src/stonks/production/pnl.py", ("tests/unit/test_pnl.py",)),
    Target(
        "client_ids", "src/stonks/execution/orders.py", ("tests/unit/test_execution_orders.py",)
    ),
    Target("orders", "src/stonks/portfolio/orders.py", ("tests/unit/test_portfolio_orders.py",)),
    Target(
        "fills",
        "src/stonks/backtest/fills.py",
        ("tests/unit/test_fills.py", "tests/unit/test_simulated_broker_fills.py"),
    ),
    Target("risk", "src/stonks/production/risk.py", ("tests/unit/test_risk.py",)),
    Target(
        "rules",
        "src/stonks/production/rules",
        (
            "tests/unit/test_risk_rules.py",
            "tests/unit/test_rules_book.py",
            "tests/unit/test_rules_circuit_breaker.py",
            "tests/unit/test_rules_position.py",
        ),
    ),
    Target(
        "ledger_sync", "src/stonks/execution/reconcile.py", ("tests/integration/test_reconcile.py",)
    ),
)


def config_text(target: Target, python: str = sys.executable) -> str:
    """The cosmic-ray config for one target."""
    tests = " ".join(target.tests)
    command = f'"{python}" -m pytest -x -q -p no:cacheprovider -p no:xdist {tests}'
    return (
        "[cosmic-ray]\n"
        f"module-path = {json.dumps(target.module)}\n"
        f"timeout = {target.timeout}\n"
        "excluded-modules = []\n"
        f"test-command = {json.dumps(command)}\n"
        "\n[cosmic-ray.distributor]\n"
        'name = "local"\n'
    )


def surviving_rate(outcomes: list[str]) -> float | None:
    """Survived over killed plus survived, in percent. Incompetent mutants
    (code that no longer imports or runs) do not count either way."""
    killed = outcomes.count("killed")
    survived = outcomes.count("survived")
    if killed + survived == 0:
        return None
    return 100.0 * survived / (killed + survived)


def outcome_name(outcome: Any) -> str:
    """``"killed"``, ``"survived"`` or ``"incompetent"``. cosmic-ray's enum
    prints as ``TestOutcome.KILLED``, so read its value."""
    return str(getattr(outcome, "value", outcome)).lower()


Span = tuple[tuple[int, int], tuple[int, int]]


def annotation_spans(source: str) -> list[Span]:
    """``((line, col), (end_line, end_col))`` of every type annotation.
    With ``from __future__ import annotations`` they are never evaluated,
    so a mutant inside one (``str | None`` to ``str + None``) is equivalent
    and only adds noise."""
    spans: list[Span] = []
    for node in ast.walk(ast.parse(source)):
        found: list[ast.expr | None] = []
        if isinstance(node, ast.arg):
            found.append(node.annotation)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            found.append(node.returns)
        elif isinstance(node, ast.AnnAssign):
            found.append(node.annotation)
        for ann in found:
            if ann is not None and ann.end_lineno is not None and ann.end_col_offset is not None:
                spans.append(((ann.lineno, ann.col_offset), (ann.end_lineno, ann.end_col_offset)))
    return spans


def in_spans(pos: tuple[int, int], spans: list[Span]) -> bool:
    return any(start <= tuple(pos) < end for start, end in spans)


def _skip_annotation_mutants(session: Path, cwd: Path) -> int:
    """Mark mutants inside type annotations as skipped; returns how many."""
    from cosmic_ray.work_db import WorkDB, use_db
    from cosmic_ray.work_item import WorkerOutcome, WorkResult

    spans: dict[Path, list[Span]] = {}
    skipped = 0
    with use_db(str(session), WorkDB.Mode.open) as db:
        for item in list(db.work_items):
            for m in item.mutations:
                path = (cwd / m.module_path).resolve()
                if path not in spans:
                    spans[path] = annotation_spans(path.read_text(encoding="utf-8"))
                if in_spans(tuple(m.start_pos), spans[path]):
                    db.set_result(
                        item.job_id,
                        WorkResult(worker_outcome=WorkerOutcome.SKIPPED),
                    )
                    skipped += 1
                    break
    return skipped


def _outcomes(session: Path) -> tuple[list[str], list[dict[str, Any]]]:
    from cosmic_ray.work_db import WorkDB, use_db

    outcomes: list[str] = []
    survivors: list[dict[str, Any]] = []
    with use_db(str(session), WorkDB.Mode.open) as db:
        for item, result in db.completed_work_items:
            outcome = outcome_name(result.test_outcome)
            outcomes.append(outcome)
            if outcome == "survived":
                for m in item.mutations:
                    survivors.append(
                        {
                            "file": Path(m.module_path).as_posix(),
                            "line": m.start_pos[0],
                            "operator": m.operator_name,
                        }
                    )
    return outcomes, survivors


def run_target(target: Target, workdir: Path) -> dict[str, Any]:
    copy = workdir / target.name
    for name in ("src", "tests"):
        shutil.copytree(ROOT / name, copy / name, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(ROOT / "pyproject.toml", copy / "pyproject.toml")
    config = copy / "cosmic-ray.toml"
    config.write_text(config_text(target))
    session = copy / "session.sqlite"
    env = {**os.environ, "PYTHONPATH": str(copy / "src")}

    def cosmic_ray(step: str) -> None:
        subprocess.run(
            [sys.executable, "-m", "cosmic_ray.cli", step, str(config), str(session)],
            cwd=copy,
            env=env,
            check=True,
        )

    cosmic_ray("init")
    skipped = _skip_annotation_mutants(session, copy)
    cosmic_ray("exec")
    outcomes, survivors = _outcomes(session)
    return {
        "target": target.name,
        "module": target.module,
        "mutants": sum(o in {"killed", "survived", "incompetent"} for o in outcomes),
        "skipped_in_annotations": skipped,
        "killed": outcomes.count("killed"),
        "survived": outcomes.count("survived"),
        "incompetent": outcomes.count("incompetent"),
        "surviving_percent": surviving_rate(outcomes),
        "survivors": survivors,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", help="comma-separated target names")
    parser.add_argument("--summary", type=Path, default=Path("mutation-summary.json"))
    args = parser.parse_args(argv)
    wanted = set(args.only.split(",")) if args.only else None
    targets = [t for t in TARGETS if wanted is None or t.name in wanted]
    results = []
    with tempfile.TemporaryDirectory(prefix="stonks-mutation-") as tmp:
        for target in targets:
            result = run_target(target, Path(tmp))
            results.append(result)
            rate = result["surviving_percent"]
            shown = "n/a" if rate is None else f"{rate:.1f}%"
            print(
                f"{target.name:<12} {result['mutants']:>5} mutants  "
                f"{result['survived']:>4} survived  {shown} surviving"
            )
    outcomes = [
        outcome
        for r in results
        for outcome in ["killed"] * r["killed"] + ["survived"] * r["survived"]
    ]
    total = surviving_rate(outcomes)
    summary = {"gate_percent": GATE_PERCENT, "surviving_percent": total, "targets": results}
    args.summary.write_text(json.dumps(summary, indent=2))
    shown = "n/a" if total is None else f"{total:.1f}%"
    print(f"total surviving: {shown} (gate {GATE_PERCENT:.0f}%)")
    return 0 if total is not None and total < GATE_PERCENT else 1


if __name__ == "__main__":
    raise SystemExit(main())
