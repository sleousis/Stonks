"""The research loop's eval cases (roadmap 22.9) in ``stonks assistant eval``.

With the scripted model each case checks the safety code: hypotheses first,
the model's cutoff, budgets, no registering, and a planted instruction in a
lab result. The checks are invariants, so a real model is judged the same
way. The checks themselves are tested against runs that break them."""

from __future__ import annotations

from datetime import date

import anyio
import pytest
from typer.testing import CliRunner

from stonks.assistant.evals import (
    ALL_CASES,
    EVAL_CUTOFF,
    RESEARCH_CASES,
    ResearchEvalRun,
    fake_model_for,
    research_invariants,
    run_research_case,
)
from stonks.assistant.research_store import ResearchProposal, ResearchSession
from stonks.cli import app


def _session(**over) -> ResearchSession:
    base = {
        "id": "rs_1",
        "owner_id": "usr_eval",
        "goal": "g",
        "universe": ("AAPL.US",),
        "universe_id": None,
        "model": "m",
        "model_cutoff": EVAL_CUTOFF,
        "prompt_version": "v",
        "max_trials": 30,
        "max_proposals": 5,
        "max_cpu_seconds": 100.0,
        "trials_used": 10,
        "cpu_seconds_used": 1.0,
        "status": "done",
        "stop_reason": None,
        "summary": None,
        "job_id": None,
        "created_at": "2026-09-01T00:00:00+00:00",
        "started_at": None,
        "finished_at": None,
    }
    base.update(over)
    return ResearchSession(**base)


def _proposal(**over) -> ResearchProposal:
    base = {
        "id": "rp_1",
        "session_id": "rs_1",
        "seq": 1,
        "hypothesis": "h" * 50,
        "premortem": "p" * 30,
        "class_path": "a:B",
        "arguments": {},
        "status": "done",
        "reason": None,
        "validation_start": "2025-01-01",
        "budget": 10,
        "lab_run_id": "lab_1",
        "verdict": "fail",
        "best_score": 0.1,
        "trials": 10,
        "cpu_seconds": 1.0,
        "outcome": {},
        "created_at": "2026-09-01T00:00:00+00:00",
        "finished_at": None,
    }
    base.update(over)
    return ResearchProposal(**base)


def _run(**over) -> ResearchEvalRun:
    base = {
        "session": _session(),
        "proposals": [_proposal()],
        "runs": [{"class_path": "a:B", "budget": 10}],
        "recorded_before_run": [True],
        "offered": [["finish_research", "propose_trial"]],
    }
    base.update(over)
    return ResearchEvalRun(**base)


@pytest.mark.parametrize("case", RESEARCH_CASES, ids=lambda c: c.name)
def test_each_research_case_passes_with_the_fake(case):
    outcome = anyio.run(lambda: run_research_case(case, fake_model_for(case)))
    assert outcome.passed, outcome.reason


def test_the_research_cases_are_in_the_eval_set():
    names = {c.name for c in ALL_CASES}
    assert {c.name for c in RESEARCH_CASES} <= names
    assert len(RESEARCH_CASES) >= 5


def test_a_clean_run_breaks_no_invariant():
    assert research_invariants(_run()) is None


def test_a_run_before_the_cutoff_is_caught():
    run = _run(proposals=[_proposal(validation_start=date(2023, 6, 1).isoformat())])
    assert "cutoff" in (research_invariants(run) or "")


def test_a_run_not_recorded_first_is_caught():
    assert "hypothesis" in (research_invariants(_run(recorded_before_run=[False])) or "")


def test_overspending_is_caught():
    run = _run(session=_session(trials_used=31))
    assert "trial budget" in (research_invariants(run) or "")
    run = _run(session=_session(cpu_seconds_used=250.0))
    assert "compute" in (research_invariants(run) or "")


def test_a_register_request_reaching_the_lab_is_caught():
    run = _run(runs=[{"class_path": "a:B", "register_strategy": True}])
    assert "regist" in (research_invariants(run) or "")


def test_extra_tools_offered_are_caught():
    run = _run(offered=[["propose_trial", "finish_research", "promote_strategy"]])
    assert "promote_strategy" in (research_invariants(run) or "")


def test_the_cli_runs_a_research_case(monkeypatch):
    monkeypatch.setenv("COLUMNS", "300")
    name = RESEARCH_CASES[0].name
    result = CliRunner().invoke(app, ["assistant", "eval", "--case", name])
    assert result.exit_code == 0, result.output
    assert name in result.output
