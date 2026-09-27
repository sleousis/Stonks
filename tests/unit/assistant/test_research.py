"""The AI research loop (roadmap 22.9): the assistant proposes hypotheses
and runs lab trials under a budget, each counted in the trial ledger.

The model proposes, deterministic code decides: every proposal is recorded
with its hypothesis before it runs, the validation window must start after
the model's training cutoff, budgets (trials, compute) are enforced in code,
and nothing registers or promotes a strategy."""

from __future__ import annotations

from datetime import date

import anyio
import pytest

from stonks.assistant.fake import FakeChatModel, Script, call
from stonks.assistant.research import (
    FINISH,
    PROPOSE,
    BudgetExceeded,
    LabExecutor,
    LabOutcome,
    ResearchConfigError,
    ResearchLoop,
    StrategyChoice,
    validation_start,
)
from stonks.assistant.research_store import ResearchStore
from stonks.assistant.settings import AssistantConfig, AssistantResearch
from stonks.store.state import SqliteState

OWNER = "usr_owner"
CUTOFF = date(2024, 6, 30)
TODAY = date(2026, 9, 1)
MOMENTUM = "stonks.strategies.examples.momentum:Momentum"
HYPOTHESIS = "Winners keep winning for months because investors underreact to news."
PREMORTEM = "It is only market beta, or costs eat the edge."


@pytest.fixture
def path(tmp_path):
    p = tmp_path / "state.sqlite"
    with SqliteState(p) as state:
        state.migrate()
    return p


@pytest.fixture
def store(path) -> ResearchStore:
    return ResearchStore(lambda: SqliteState(path))


class FakeExecutor(LabExecutor):
    """Canned lab results. Records each proposal and what the store held
    when it ran."""

    def __init__(self, store: ResearchStore, *, on_run=None, verdict: str = "fail") -> None:
        self.store = store
        self.ran: list[dict] = []
        self.seen_rows: list[list[dict]] = []
        self.unscored_calls: list[str] = []
        self.on_run = on_run
        self.verdict = verdict

    def strategies(self) -> list[StrategyChoice]:
        return [StrategyChoice(MOMENTUM, "time-series momentum")]

    def run(self, proposal, *, session, checkpoint) -> LabOutcome:
        self.seen_rows.append([p.__dict__ for p in self.store.proposals(session.id)])
        self.ran.append(proposal.model_dump())
        if self.on_run is not None:
            self.on_run(checkpoint)
        checkpoint()
        return LabOutcome(
            run_id=f"lab_{len(self.ran)}",
            verdict=self.verdict,
            best_score=0.4,
            best_params={"lookback": 60},
            n_trials_run=proposal.budget,
            n_trials_class=proposal.budget,
            n_trials_family=proposal.budget * len(self.ran),
            reports=[{"test_id": "oos", "passed": False, "notes": "PSR 0.4 < 0.95"}],
        )

    def count_unscored(self, family: str) -> int:
        self.unscored_calls.append(family)
        return 7


def _proposal(**over) -> dict:
    args = {
        "hypothesis": HYPOTHESIS,
        "premortem": PREMORTEM,
        "class_path": MOMENTUM,
        "start": "2023-01-01",
        "end": "2026-06-30",
        "budget": 10,
        "train_ratio": 0.5,
    }
    args.update(over)
    return args


def _config(**research) -> AssistantConfig:
    base = {"model_cutoff": CUTOFF}
    base.update(research)
    return AssistantConfig(base_url="http://x", research=AssistantResearch(**base))


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _run(store, turns, *, config=None, executor=None, clock=None, workers=1, frozen=None):
    cfg = config or _config()
    session = store.create_session(
        OWNER,
        "find a momentum edge",
        universe=["AAPL.US", "MSFT.US"],
        model="fake",
        model_cutoff=cfg.research.model_cutoff,
        budget=cfg.research,
    )
    model = FakeChatModel(list(turns))
    ex = executor or FakeExecutor(store)
    loop = ResearchLoop(
        model,
        ex,
        store,
        cfg,
        workers=workers,
        clock=clock or Clock(),
        today=lambda: TODAY,
        frozen=frozen or (lambda: None),
    )
    final = anyio.run(loop.run, session.id)
    return final, ex, model


# ---- the cutoff ----------------------------------------------------------------------


def test_validation_start_matches_the_lab_split():
    assert validation_start(date(2024, 1, 1), date(2024, 12, 31), 0.5) == date(2024, 7, 2)


def test_a_validation_window_before_the_cutoff_never_runs(store):
    final, ex, _ = _run(
        store,
        [
            Script(calls=(call(PROPOSE, _proposal(start="2020-01-01", end="2024-12-31")),)),
            Script(text="ok"),
        ],
    )
    assert ex.ran == []
    (p,) = store.proposals(final.id)
    assert p.status == "rejected"
    assert "cutoff" in (p.reason or "")
    assert p.hypothesis == HYPOTHESIS
    assert final.trials_used == 0


def test_a_validation_window_after_the_cutoff_runs(store):
    final, ex, _ = _run(store, [Script(calls=(call(PROPOSE, _proposal()),)), Script(text="ok")])
    assert len(ex.ran) == 1
    (p,) = store.proposals(final.id)
    assert p.status == "done" and p.lab_run_id == "lab_1"
    assert p.validation_start is not None and p.validation_start > CUTOFF.isoformat()
    assert final.status == "done" and final.trials_used == 10


def test_no_cutoff_means_no_loop(store, path):
    cfg = AssistantConfig(base_url="http://x")
    with pytest.raises(ResearchConfigError, match="model_cutoff"):
        ResearchLoop(FakeChatModel([]), FakeExecutor(store), store, cfg)


def test_a_window_ending_in_the_future_is_rejected(store):
    final, ex, _ = _run(
        store, [Script(calls=(call(PROPOSE, _proposal(end="2027-01-01")),)), Script(text="ok")]
    )
    assert ex.ran == []
    assert "future" in (store.proposals(final.id)[0].reason or "")


# ---- recorded before it runs -----------------------------------------------------------


def test_every_proposal_is_recorded_with_its_hypothesis_before_it_runs(store):
    final, ex, _ = _run(store, [Script(calls=(call(PROPOSE, _proposal()),)), Script(text="ok")])
    (rows,) = ex.seen_rows
    assert [(r["status"], r["hypothesis"], r["premortem"]) for r in rows] == [
        ("running", HYPOTHESIS, PREMORTEM)
    ]


def test_a_short_hypothesis_is_rejected(store):
    final, ex, _ = _run(
        store,
        [Script(calls=(call(PROPOSE, _proposal(hypothesis="it goes up")),)), Script(text="ok")],
    )
    assert ex.ran == []
    assert "hypothesis" in (store.proposals(final.id)[0].reason or "")


def test_an_unknown_strategy_is_rejected(store):
    final, ex, _ = _run(
        store,
        [Script(calls=(call(PROPOSE, _proposal(class_path="evil:Thing")),)), Script(text="ok")],
    )
    assert ex.ran == []
    assert "strategy" in (store.proposals(final.id)[0].reason or "")


def test_bad_arguments_are_recorded_and_rejected(store):
    final, ex, _ = _run(
        store, [Script(calls=(call(PROPOSE, _proposal(budget="lots")),)), Script(text="ok")]
    )
    assert ex.ran == []
    (p,) = store.proposals(final.id)
    assert p.status == "rejected" and "invalid" in (p.reason or "")
    assert p.arguments["budget"] == "lots"


# ---- governance ----------------------------------------------------------------------


@pytest.mark.parametrize("key", ["register_strategy", "register_if_passes", "promote", "confirm"])
def test_a_proposal_that_asks_to_register_is_rejected(store, key):
    final, ex, _ = _run(
        store, [Script(calls=(call(PROPOSE, _proposal(**{key: True})),)), Script(text="ok")]
    )
    assert ex.ran == []
    assert "governance" in (store.proposals(final.id)[0].reason or "")


def test_the_executor_never_sees_a_register_field(store):
    _, ex, _ = _run(store, [Script(calls=(call(PROPOSE, _proposal()),)), Script(text="ok")])
    assert not any(k.startswith("register") for k in ex.ran[0])


# ---- budgets -------------------------------------------------------------------------


def test_a_proposal_over_the_per_proposal_budget_is_rejected(store):
    final, ex, _ = _run(
        store,
        [Script(calls=(call(PROPOSE, _proposal(budget=11)),)), Script(text="ok")],
        config=_config(max_budget_per_proposal=10),
    )
    assert ex.ran == []
    assert "budget" in (store.proposals(final.id)[0].reason or "")


def test_the_session_trial_budget_is_enforced(store):
    final, ex, _ = _run(
        store,
        [
            Script(calls=(call(PROPOSE, _proposal(budget=10)),)),
            Script(calls=(call(PROPOSE, _proposal(budget=10)),)),
            Script(text="ok"),
        ],
        config=_config(max_trials=15),
    )
    assert len(ex.ran) == 1
    statuses = [p.status for p in store.proposals(final.id)]
    assert statuses == ["done", "rejected"]
    assert "trials left" in (store.proposals(final.id)[1].reason or "")
    assert final.trials_used == 10


def test_the_session_ends_when_the_trial_budget_is_used(store):
    final, ex, model = _run(
        store,
        [Script(calls=(call(PROPOSE, _proposal(budget=10)),)), Script(text="never asked")],
        config=_config(max_trials=10),
    )
    assert final.status == "stopped" and "trial budget" in (final.stop_reason or "")
    assert len(model.requests) == 1


def test_the_proposal_count_is_bounded(store):
    turns = [Script(calls=(call(PROPOSE, _proposal(hypothesis="x")),)) for _ in range(6)]
    final, _, model = _run(store, turns, config=_config(max_proposals=3))
    assert len(store.proposals(final.id)) == 3
    assert final.status == "stopped" and "proposals" in (final.stop_reason or "")


def test_the_compute_budget_stops_a_running_trial(store):
    clock = Clock()

    def burn(checkpoint):
        clock.now += 100.0  # 100 s of wall time on 4 workers = 400 compute seconds
        checkpoint()

    ex = FakeExecutor(store, on_run=burn)
    final, ex, _ = _run(
        store,
        [Script(calls=(call(PROPOSE, _proposal(budget=10)),)), Script(text="ok")],
        config=_config(max_cpu_seconds=300.0),
        executor=ex,
        clock=clock,
        workers=4,
    )
    (p,) = store.proposals(final.id)
    assert p.status == "stopped"
    assert ex.unscored_calls == [final.id]  # the lost trials are counted in the ledger
    assert p.trials == 10
    assert final.status == "stopped" and "compute" in (final.stop_reason or "")
    assert final.cpu_seconds_used == pytest.approx(400.0)


def test_a_session_out_of_compute_runs_nothing_more(store):
    clock = Clock()

    def burn(checkpoint):
        clock.now += 50.0

    ex = FakeExecutor(store, on_run=burn)
    final, ex, _ = _run(
        store,
        [
            Script(calls=(call(PROPOSE, _proposal(budget=5)),)),
            Script(calls=(call(PROPOSE, _proposal(budget=5)),)),
        ],
        config=_config(max_cpu_seconds=50.0),
        executor=ex,
        clock=clock,
    )
    assert len(ex.ran) == 1
    assert final.status == "stopped"


def test_budget_exceeded_is_a_stop_reason():
    assert issubclass(BudgetExceeded, RuntimeError)


# ---- the rest of the envelope ------------------------------------------------------------


def test_a_frozen_assistant_stops_before_running(store):
    final, ex, _ = _run(
        store,
        [Script(calls=(call(PROPOSE, _proposal()),))],
        frozen=lambda: "frozen: a burst of writes",
    )
    assert ex.ran == []
    assert final.status == "stopped" and "frozen" in (final.stop_reason or "")


def test_finish_records_the_summary(store):
    final, _, _ = _run(
        store,
        [
            Script(calls=(call(PROPOSE, _proposal()),)),
            Script(calls=(call(FINISH, {"summary": "No edge survived."}),)),
        ],
    )
    assert final.status == "done" and final.summary == "No edge survived."


def test_a_model_error_fails_the_session(store):
    final, _, _ = _run(store, [Script(error="boom")])
    assert final.status == "failed" and "boom" in (final.stop_reason or "")


def test_lab_results_reach_the_model_as_untrusted_data(store):
    _, _, model = _run(store, [Script(calls=(call(PROPOSE, _proposal()),)), Script(text="ok")])
    messages, tools, _ = model.requests[1]
    tool_messages = [m for m in messages if m.role == "tool"]
    assert tool_messages and 'trust="untrusted"' in tool_messages[0].content
    assert set(tools) == {PROPOSE, FINISH}


def test_the_prompt_names_the_cutoff_and_the_budget(store):
    _, _, model = _run(store, [Script(text="nothing to try")])
    messages, _, _ = model.requests[0]
    text = "\n".join(m.content for m in messages)
    assert CUTOFF.isoformat() in text and "200" in text and MOMENTUM in text


def test_the_lab_failing_is_recorded_and_the_loop_goes_on(store):
    def crash(checkpoint):
        raise RuntimeError("no bars")

    ex = FakeExecutor(store, on_run=crash)
    final, ex, model = _run(
        store,
        [Script(calls=(call(PROPOSE, _proposal()),)), Script(text="it failed")],
        executor=ex,
    )
    (p,) = store.proposals(final.id)
    assert p.status == "failed" and "no bars" in (p.reason or "")
    assert ex.unscored_calls == [final.id]
    assert final.status == "done" and final.trials_used == 10  # the whole budget, to be safe
