"""Roadmap 23.9: the verify report's judgement and fingerprint diffs."""

from __future__ import annotations

import math

from stonks.lab.manifest import fingerprint_changes
from stonks.lab.verify import VerifyReport


def _report(stored, current, tol=0.05) -> VerifyReport:
    return VerifyReport(
        target="t",
        kind="run",
        class_path="m:C",
        run_id="t",
        objective="sharpe",
        tolerance=tol,
        stored_score=stored,
        current_score=current,
    )


def test_moved_beyond_the_tolerance_only() -> None:
    assert _report(1.0, 1.04).moved is False
    assert _report(1.0, 1.06).moved is True
    assert _report(1.0, 0.9).moved is True
    assert math.isclose(_report(1.0, 1.06).score_delta or 0.0, 0.06)


def test_a_result_that_can_no_longer_be_scored_has_moved() -> None:
    assert _report(1.0, None).moved is True
    assert _report(None, None).moved is False


def test_fingerprint_changes_name_the_tickers() -> None:
    before = {"tickers": {"A": {"bars_hash": "1"}, "B": {"bars_hash": "2"}}, "references": {}}
    after = {
        "tickers": {"A": {"bars_hash": "1"}, "B": {"bars_hash": "3"}},
        "references": {"SPY": {"bars_hash": "x"}},
    }
    assert fingerprint_changes(before, after) == ["B", "SPY"]
    assert fingerprint_changes(before, before) == []
