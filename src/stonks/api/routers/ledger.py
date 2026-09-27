"""The trial ledger (BL-04): every lab run with its hypothesis, premortem
and trials, so the multiple-testing context (P2) can be read back."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query

from stonks.api.deps import PageDep, PrincipalDep, ServicesDep, needs
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.app.pagination import Page
from stonks.app.trial_ledger import LedgerRunDetail, LedgerRunView
from stonks.auth import Permission

router = APIRouter(prefix="/api/lab/ledger", tags=["lab"], responses=PROBLEM_RESPONSES)


@router.get(
    "",
    response_model=Page[LedgerRunView],
    operation_id="listLedgerRuns",
    dependencies=needs(Permission.LAB_RUN),
)
def list_ledger_runs(
    services: ServicesDep,
    principal: PrincipalDep,
    page: PageDep,
    strategy_class: Annotated[
        str | None, Query(max_length=200, description="only runs of this module:Class")
    ] = None,
) -> Page[LedgerRunView]:
    """Recorded lab runs, newest first: the strategy class, hypothesis,
    tuner and budget, how many trials ran and the verdict. Shared across
    users, like the strategy list, because trial counts are."""
    return services.ledger.runs(
        principal, strategy_class=strategy_class, limit=page.limit, offset=page.offset
    )


@router.get(
    "/{run_id}",
    response_model=LedgerRunDetail,
    operation_id="getLedgerRun",
    dependencies=needs(Permission.LAB_RUN),
)
def get_ledger_run(
    run_id: Annotated[str, Path(max_length=80)], services: ServicesDep, principal: PrincipalDep
) -> LedgerRunDetail:
    """One recorded lab run with every trial (parameters, score, status)
    and the trial count of its class across all runs."""
    return services.ledger.run(principal, run_id)
