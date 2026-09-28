"""Lab verify service (roadmap 23.9): rerun lab results from their stored
manifests and report whether they moved.

``verify`` runs in this process (the CLI and the job handler). The API and
the weekly ``lab_verify`` scheduler action queue it as a job of kind
``lab_verify``. With ``alert`` the moved results raise one operator alert
through ``[notify]``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from stonks.app.context import AppContext
from stonks.app.errors import ConflictError, NotFoundError
from stonks.app.jobs import Job, JobContext, JobRunner
from stonks.lab.verify import VerifyReport, resolve_target, verify
from stonks.logging import get_logger

_log = get_logger("stonks.app.lab_verify")

VERIFY_JOB = "lab_verify"


class VerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Lab run ids or strategy ids. Empty: every strategy of
    #: ``[lab.verify].statuses``.
    targets: list[str] = Field(default_factory=list, max_length=200)
    #: Overrides ``[lab.verify].tolerance``.
    tolerance: float | None = Field(default=None, ge=0.0)
    #: Raise an operator alert when a result moved (the weekly job does).
    alert: bool = False


class VerifyReportView(BaseModel):
    target: str
    kind: str
    class_path: str
    run_id: str | None
    objective: str | None
    tolerance: float
    stored_score: float | None
    current_score: float | None
    score_delta: float | None
    #: The score moved beyond the tolerance, or could no longer be scored.
    moved: bool
    #: Bars or corporate actions the run read changed since (a restatement).
    data_changed: bool | None
    changed_tickers: list[str]
    #: Tickers whose point-in-time statement versions changed (restated).
    restated_tickers: list[str] = Field(default_factory=list)
    config_changed: bool | None
    code_changed: bool | None
    error: str | None


class VerifyResultView(BaseModel):
    checked: int
    moved: list[str]
    reports: list[VerifyReportView]
    alerted: bool = False


class LabVerifyService:
    def __init__(
        self,
        context: AppContext,
        runner: JobRunner | None = None,
        *,
        objectives: dict[str, Callable[[], Any]] | None = None,
    ) -> None:
        self._ctx = context
        self._runner = runner
        self._objectives = objectives
        if runner is not None:
            runner.register(VERIFY_JOB, self._handle, cancellable=True, operation=False)

    def submit(self, request: VerifyRequest, *, owner_id: str | None = None) -> Job:
        if self._runner is None:  # pragma: no cover - wiring error
            raise ConflictError("no job runner: verify in this process instead")
        return self._runner.submit(VERIFY_JOB, request.model_dump(mode="json"), owner_id=owner_id)

    def verify(
        self, request: VerifyRequest, *, checkpoint: Callable[[], None] | None = None
    ) -> VerifyResultView:
        """Rerun each target now. ``NotFoundError`` for an unknown explicit
        target."""
        settings = self._ctx.settings
        cfg = settings.lab.verify
        tolerance = cfg.tolerance if request.tolerance is None else request.tolerance
        reports: list[VerifyReport] = []
        with self._ctx.state() as state, self._ctx.lake() as lake:
            registry = self._ctx.registry_on(state)
            artifacts = settings.registry.artifacts_dir
            targets = list(request.targets) or [
                h.id for status in cfg.statuses for h in registry.list_all(status)
            ]
            for key in targets:
                if checkpoint is not None:
                    checkpoint()
                try:
                    target = resolve_target(state, registry, artifacts, key)
                except KeyError:
                    if request.targets:
                        raise NotFoundError(f"no lab run or strategy {key!r}") from None
                    continue
                reports.append(
                    verify(
                        target,
                        lake=lake,
                        settings=settings,
                        tolerance=tolerance,
                        objectives=self._objectives,
                    )
                )
        moved = [r.target for r in reports if r.moved]
        alerted = bool(request.alert and moved) and self._alert(reports)
        return VerifyResultView(
            checked=len(reports),
            moved=moved,
            reports=[VerifyReportView.model_validate(r.as_dict()) for r in reports],
            alerted=alerted,
        )

    def _alert(self, reports: list[VerifyReport]) -> bool:
        from stonks.notify import Notification, notifier_from_settings

        moved = [r for r in reports if r.moved]
        lines = [
            f"{r.target}: score {_num(r.stored_score)} -> {_num(r.current_score)}"
            + (f", data changed {', '.join(r.changed_tickers[:5])}" if r.changed_tickers else "")
            + (f", restated {', '.join(r.restated_tickers[:5])}" if r.restated_tickers else "")
            for r in moved
        ]
        try:
            notifier_from_settings(self._ctx.settings).notify(
                Notification(
                    level="warning",
                    title=f"lab verify: {len(moved)} result(s) moved",
                    message="; ".join(lines),
                    fields={"moved": [r.target for r in moved]},
                )
            )
        except Exception as exc:  # notifiers should not raise, guard anyway
            _log.error("lab.verify.alert_failed", error=str(exc))
            return False
        return True

    def _handle(self, params: dict[str, Any], ctx: JobContext) -> VerifyResultView:
        return self.verify(VerifyRequest.model_validate(params), checkpoint=ctx.check_cancelled)


def _num(value: float | None) -> str:
    return "-" if value is None else f"{value:.3f}"
