"""Survival-suite scaffolding. SurvivalTest is defined in core.protocols as a
structural Protocol; this module just holds the composition helper."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from stonks.core.protocols import Strategy, SurvivalReport, SurvivalTest


class SurvivalSuite:
    def __init__(self, tests: Sequence[SurvivalTest]) -> None:
        self._tests = list(tests)

    def run(self, strategy: Strategy, context: Any) -> list[SurvivalReport]:
        return [t.run(strategy, context) for t in self._tests]

    @property
    def tests(self) -> list[SurvivalTest]:
        return list(self._tests)
