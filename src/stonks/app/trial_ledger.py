"""TrialLedgerService: read the trial ledger back (BL-04, roadmap 18.7).

Every lab run records its hypothesis, premortem, tuner, budget, dataset and
each trial before and while it tunes (:mod:`stonks.lab.trials`). The
multiple-testing rules (P2) count those trials across every run of a
strategy class, so the ledger is a shared research record like the strategy
registry: anyone who may run the lab may read all of it.
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from stonks.app.context import AppContext
from stonks.app.errors import NotFoundError
from stonks.app.pagination import Page
from stonks.auth.policy import Permission, require
from stonks.auth.principal import Principal
from stonks.lab.trials import TrialLedger


#: The run's robustness outcome, apart from how its trials ran (visual audit
#: M5): ``survived`` or ``did_not_survive`` the robustness tests, ``error``
#: when the run crashed, ``running`` while it goes, ``stopped`` when it
#: ended without a verdict. Show this as the run's status.
Robustness = Literal["running", "survived", "did_not_survive", "error", "stopped"]

#: How one trial ran: ``ran`` (a finite score) or ``error`` (no score). It
#: says nothing about robustness.
TrialOutcome = Literal["ran", "error"]

_ROBUSTNESS: dict[str, Robustness] = {
    "pass": "survived",
    "fail": "did_not_survive",
    "error": "error",
}


def robustness_of(verdict: str | None, finished: bool) -> Robustness:
    """The run's :data:`Robustness` from its stored verdict."""
    if verdict in _ROBUSTNESS:
        return _ROBUSTNESS[verdict]
    return "stopped" if finished else "running"


class LedgerRunView(BaseModel):
    """One recorded lab run: what was tested, why, and how it came out."""

    id: str
    strategy_class: str
    hypothesis: str | None
    premortem: str | None
    tuner: str | None
    objective: str | None
    budget: int | None
    seed: int | None
    started_at: datetime
    finished_at: datetime | None
    #: ``None`` while the run is going, or when it stopped without one.
    verdict: Literal["pass", "fail", "error"] | None
    n_trials: int = Field(description="Trials this run evaluated.")
    n_failed: int = Field(description="Trials that failed (no finite score).")
    robustness: Robustness = Field(
        description="The run's status: did the strategy survive the robustness tests. "
        "Independent of the trial counts."
    )
    trials_ran: int = Field(description="Trials that ran to a score.")
    trials_errored: int = Field(description="Trials that ended with no score (same as n_failed).")
    best_score: float | None = Field(description="Best finite objective score, null if none.")
    universe_id: str | None = None
    tickers: int = Field(default=0, description="Tickers in the dataset.")
    interval: str | None = None
    start: str | None = None
    end: str | None = None


class LedgerTrialView(BaseModel):
    trial_index: int
    params: dict[str, Any]
    score: float | None = Field(description="Objective score, null for a failed trial.")
    n_bars: int | None
    status: Literal["ok", "failed"]
    outcome: TrialOutcome = Field(
        description="How the trial ran (ran or error); not a robustness verdict."
    )


class LedgerRunDetail(LedgerRunView):
    trials: list[LedgerTrialView]
    n_trials_class: int = Field(
        description="Trials of this strategy class across every recorded run (what P2 counts)."
    )


class TrialLedgerService:
    def __init__(self, context: AppContext) -> None:
        self._ctx = context

    def _ledger(self, state: Any) -> TrialLedger:
        return TrialLedger(state, self._ctx.settings.registry.artifacts_dir)

    def runs(
        self,
        principal: Principal,
        *,
        strategy_class: str | None = None,
        limit: int,
        offset: int,
    ) -> Page[LedgerRunView]:
        """Recorded lab runs, newest first, optionally of one class."""
        require(principal, Permission.LAB_RUN)
        where, params = (
            ("WHERE r.strategy_class = ?", [strategy_class])
            if strategy_class
            else (
                "",
                [],
            )
        )
        with self._ctx.state() as state:
            total = int(state.sql(f"SELECT COUNT(*) FROM lab_runs r {where}", params)[0][0])
            rows = state.sql(
                "SELECT r.*, COUNT(t.trial_index) AS n_trials,"
                " SUM(CASE WHEN t.status = 'failed' THEN 1 ELSE 0 END) AS n_failed,"
                " MAX(t.score) AS best_score"
                f" FROM lab_runs r LEFT JOIN lab_trials t ON t.run_id = r.id {where}"
                " GROUP BY r.id ORDER BY r.started_at DESC, r.id DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            )
        items = [_run_view(dict(r)) for r in rows]
        return Page[LedgerRunView](items=items, total=total, limit=limit, offset=offset)

    def run(self, principal: Principal, run_id: str) -> LedgerRunDetail:
        """One run with every trial and the class's cumulative trial count."""
        require(principal, Permission.LAB_RUN)
        with self._ctx.state() as state:
            rows = state.sql(
                "SELECT r.*, COUNT(t.trial_index) AS n_trials,"
                " SUM(CASE WHEN t.status = 'failed' THEN 1 ELSE 0 END) AS n_failed,"
                " MAX(t.score) AS best_score"
                " FROM lab_runs r LEFT JOIN lab_trials t ON t.run_id = r.id"
                " WHERE r.id = ? GROUP BY r.id",
                [run_id],
            )
            if not rows:
                raise NotFoundError(f"no lab run {run_id!r} in the trial ledger")
            ledger = self._ledger(state)
            trials = ledger.trials(run_id)
            n_class = ledger.n_trials(rows[0]["strategy_class"])
        view = _run_view(dict(rows[0]))
        return LedgerRunDetail(
            **view.model_dump(),
            trials=[
                LedgerTrialView(
                    trial_index=t.trial_index,
                    params=t.params,
                    score=t.score if math.isfinite(t.score) else None,
                    n_bars=t.n_bars,
                    status=t.status,
                    outcome="ran" if t.status == "ok" else "error",
                )
                for t in trials
            ],
            n_trials_class=n_class,
        )


def _run_view(row: dict[str, Any]) -> LedgerRunView:
    dataset = json.loads(row.get("dataset_json") or "{}")
    best = row.get("best_score")
    universe = dataset.get("universe") or []
    n_trials = int(row.get("n_trials") or 0)
    n_failed = int(row.get("n_failed") or 0)
    return LedgerRunView(
        id=row["id"],
        strategy_class=row["strategy_class"],
        hypothesis=row.get("hypothesis"),
        premortem=row.get("premortem"),
        tuner=row.get("tuner"),
        objective=row.get("objective"),
        budget=row.get("budget"),
        seed=row.get("seed"),
        started_at=row["started_at"],
        finished_at=row.get("finished_at"),
        verdict=row.get("verdict"),
        n_trials=n_trials,
        n_failed=n_failed,
        robustness=robustness_of(row.get("verdict"), row.get("finished_at") is not None),
        trials_ran=n_trials - n_failed,
        trials_errored=n_failed,
        best_score=float(best) if best is not None and math.isfinite(best) else None,
        universe_id=dataset.get("universe_id"),
        tickers=len(universe) if isinstance(universe, list) else 0,
        interval=dataset.get("interval"),
        start=_edge(dataset.get("full_window"), 0),
        end=_edge(dataset.get("full_window"), -1),
    )


def _edge(window: Any, i: int) -> str | None:
    return str(window[i]) if isinstance(window, list) and window else None
