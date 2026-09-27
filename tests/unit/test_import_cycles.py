"""Modules import cleanly on their own, in a fresh interpreter.

A test file that imports one module first must not trip a circular import
that the full suite hides because another test loaded the modules in a
friendlier order.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

MODULES = [
    "stonks.production.risk",
    "stonks.portfolio.pipeline",
    "stonks.config",
    "stonks.accounts",
    "stonks.production.tick",
]


@pytest.mark.parametrize("module", MODULES)
def test_module_imports_alone(module: str) -> None:
    proc = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
