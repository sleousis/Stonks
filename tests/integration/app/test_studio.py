"""StudioService: drafts CRUD, validation, backtest / lab-run jobs,
registration and enable / disable, plus the opt-in code strategies."""

from __future__ import annotations

import copy
import sys
from datetime import date

import pytest

from stonks.app.context import AppContext
from stonks.app.errors import ConflictError, NotFoundError, ValidationError
from stonks.app.services import Services, default_strategy_sources
from stonks.app.studio import (
    STUDIO_BACKTEST_JOB,
    USER_MODULE_PREFIX,
    CodeStrategiesDisabledError,
    DraftBacktestRequest,
    DraftCreate,
    DraftLabRunRequest,
    DraftUpdate,
    RuleStrategySource,
    StudioService,
    ValidateRequest,
    studio_service,
    user_strategies_dir,
)
from stonks.strategies.rule_based import RULE_STRATEGY_CLASS_PATH
from stonks.strategies.rules import TEMPLATES

SMA = TEMPLATES["sma_trend_following"].spec

# Fast trend rule that fits the ~6 months of seeded bars.
TREND = {
    "version": 1,
    "name": "trend",
    "indicators": [
        {"id": "fast", "kind": "sma", "period": 5},
        {"id": "slow", "kind": "sma", "period": 20},
    ],
    "entry": {
        "type": "compare",
        "left": {"type": "indicator", "id": "fast"},
        "op": ">",
        "right": {"type": "indicator", "id": "slow"},
    },
    "rank": {"by": "fast"},
}

GOOD_CODE = """
from stonks.core.params import ParameterSpec
from stonks.strategies.base import BaseStrategy


class AlwaysUp(BaseStrategy):
    id = "always_up"

    @classmethod
    def parameter_spec(cls):
        return [ParameterSpec(name="edge", kind="float", default=0.01, bounds=(0.0, 1.0))]

    def estimate_return(self, ticker, as_of, lake):
        return float(self.params["edge"])

    def decide(self, my_picks, portfolio, prices, as_of):
        from stonks.core.types import Order
        orders = []
        for _, ticker in my_picks[:1]:
            price = prices.get(ticker)
            if price and portfolio.cash > 0 and ticker not in portfolio.positions:
                orders.append(Order(client_id=f"up:{ticker}:{as_of}", ticker=ticker,
                                    side="buy", quantity=portfolio.cash * 0.5 / price))
        return orders
"""


@pytest.fixture(autouse=True)
def _forget_user_modules():
    yield
    for name in [m for m in sys.modules if m.startswith(USER_MODULE_PREFIX)]:
        del sys.modules[name]


@pytest.fixture
def svc(settings, seeded, fake_source):
    ctx = AppContext(settings, source_factory=lambda: fake_source)
    services = Services.create(
        ctx, strategy_sources=[*default_strategy_sources(), RuleStrategySource()]
    )
    services.start()
    yield services
    services.shutdown()


@pytest.fixture
def studio(svc) -> StudioService:
    return studio_service(svc)


@pytest.fixture
def code_on(settings):
    settings.api.allow_code_strategies = True
    return settings


def _bt(**kw) -> DraftBacktestRequest:
    return DraftBacktestRequest(
        **{"universe": ["UP.US", "DOWN.US"], "start": date(2025, 11, 1), "end": date(2026, 4, 1)}
        | kw
    )


def _lab(**kw) -> DraftLabRunRequest:
    base = {
        "universe": ["UP.US", "DOWN.US"],
        "start": date(2025, 10, 1),
        "end": date(2026, 4, 1),
        "budget": 1,
        "survival_tests": ["oos"],
    }
    return DraftLabRunRequest(**base | kw)


# ---- catalog-ish reads ------------------------------------------------------


def test_studio_service_is_cached_per_services(svc):
    assert studio_service(svc) is studio_service(svc)


def test_templates_and_schema(studio):
    templates = studio.templates()
    assert {t.id for t in templates} == set(TEMPLATES)
    assert all(t.spec["version"] == 1 for t in templates)
    assert studio.schema()["title"] == "RuleSpec"


def test_validate_spec_without_a_draft(studio):
    ok = studio.validate_spec(SMA)
    assert ok.valid and ok.issues == []
    bad = studio.validate_spec({**SMA, "rank": {"by": "nope"}})
    assert not bad.valid
    assert [i.path for i in bad.issues] == ["rank.by"]


# ---- drafts CRUD ------------------------------------------------------------


def test_draft_crud(studio):
    draft = studio.create_draft(DraftCreate(name="My trend", spec=TREND))
    assert draft.kind == "rule"
    assert draft.status == "draft"
    assert draft.registered_strategy_id is None
    assert draft.source_code is None
    assert studio.get_draft(draft.id) == draft

    updated = studio.update_draft(draft.id, DraftUpdate(name="Renamed", spec=SMA))
    assert updated.name == "Renamed"
    assert updated.spec["name"] == "SMA trend following"
    assert updated.updated_at >= draft.updated_at

    page = studio.list_drafts(limit=10, offset=0)
    assert page.total == 1 and page.items[0].id == draft.id

    removed = studio.delete_draft(draft.id)
    assert removed.id == draft.id
    with pytest.raises(NotFoundError):
        studio.get_draft(draft.id)
    with pytest.raises(NotFoundError):
        studio.update_draft(draft.id, DraftUpdate(name="x"))


def test_drafts_may_hold_work_in_progress_specs(studio):
    draft = studio.create_draft(DraftCreate(name="wip", spec={"version": 1}))
    result = studio.validate_draft(draft.id, ValidateRequest())
    assert not result.valid
    assert result.smoke is None
    assert {i.path for i in result.issues} >= {"indicators", "entry", "rank"}


def test_rule_draft_rejects_source_code():
    with pytest.raises(ValueError):
        DraftCreate(name="x", kind="rule", spec=TREND, source_code="print(1)")
    with pytest.raises(ValueError):
        DraftCreate(name="x", kind="code")


# ---- validation + smoke -----------------------------------------------------


def test_validate_runs_smoke_on_sample_data_by_default(studio):
    draft = studio.create_draft(DraftCreate(name="t", spec=SMA))
    result = studio.validate_draft(draft.id, ValidateRequest())
    assert result.valid, result.issues
    assert result.smoke is not None
    assert result.smoke.ok
    assert result.smoke.data == "sample"
    assert result.smoke.evaluations > 0


def test_validate_runs_smoke_on_the_lake_for_given_tickers(studio):
    draft = studio.create_draft(DraftCreate(name="t", spec=TREND))
    result = studio.validate_draft(
        draft.id, ValidateRequest(tickers=["UP.US", "DOWN.US"], as_of=date(2026, 3, 2), bars=10)
    )
    assert result.valid and result.smoke.ok
    assert result.smoke.data == "lake"
    assert result.smoke.evaluations == 20
    assert result.smoke.signals == 10  # UP.US passes entry on every bar


# ---- backtest + lab run -----------------------------------------------------


def test_backtest_rule_draft_goes_through_the_lab_job(svc, studio):
    draft = studio.create_draft(DraftCreate(name="t", spec=TREND))
    job = studio.submit_backtest(draft.id, _bt())
    assert job.kind == "backtest"
    assert job.params["strategy"]["class_path"] == RULE_STRATEGY_CLASS_PATH
    done = svc.jobs.wait(job.id, timeout=60)
    assert done.status == "succeeded", done.error
    assert done.result["final_return"] > 0.1


def test_backtest_invalid_draft_is_rejected_before_queueing(svc, studio):
    draft = studio.create_draft(DraftCreate(name="t", spec={**TREND, "rank": {"by": "zz"}}))
    with pytest.raises(ValidationError, match="rank.by"):
        studio.submit_backtest(draft.id, _bt())
    assert svc.jobs.list(limit=10, offset=0).total == 0


def test_a_draft_is_registered_at_most_once(svc, studio):
    draft = studio.create_draft(DraftCreate(name="twice", spec=TREND))
    job = studio.submit_lab_run(draft.id, _lab(register_strategy=True))
    try:
        direct = studio.register_draft(draft.id).registered_strategy_id
    except ConflictError:
        direct = None
    done = svc.jobs.wait(job.id, timeout=120)
    if direct is None:  # the job registered first
        assert done.status == "succeeded"
        winner = done.result["registered_strategy_id"]
    else:
        assert done.status == "failed"
        assert "already registered" in done.error
        winner = direct
    assert studio.get_draft(draft.id).registered_strategy_id == winner
    assert [s.id for s in svc.strategies.list(limit=50, offset=0).items].count(winner) == 1
    assert len([s for s in svc.strategies.list(limit=50, offset=0).items if "twice" in s.id]) == 1


def test_lab_run_uses_the_draft_spec_and_can_register(svc, studio):
    draft = studio.create_draft(DraftCreate(name="Lab trend", spec=TREND))
    job = studio.submit_lab_run(draft.id, _lab(register_strategy=True))
    done = svc.jobs.wait(job.id, timeout=120)
    assert done.status == "succeeded", done.error
    assert done.result["class_path"] == RULE_STRATEGY_CLASS_PATH
    assert done.result["best_params"]["spec"]["name"] == "trend"
    sid = done.result["registered_strategy_id"]
    detail = svc.strategies.get(sid)
    assert detail.status == "shadow"
    assert detail.params["spec"]["name"] == "trend"
    assert [r.test_id for r in detail.survival_reports] == ["oos"]
    refreshed = studio.get_draft(draft.id)
    assert refreshed.status == "registered"
    assert refreshed.registered_strategy_id == sid
    assert refreshed.strategy_status == "shadow"


# ---- register / enable / disable --------------------------------------------


def test_register_enable_disable(svc, studio):
    draft = studio.create_draft(DraftCreate(name="My Trend!", spec=TREND))
    with pytest.raises(ConflictError):
        studio.enable(draft.id)
    registered = studio.register_draft(draft.id)
    sid = registered.registered_strategy_id
    assert sid and sid.startswith("my_trend_")
    assert registered.status == "registered"
    assert registered.strategy_status == "shadow"
    with svc.context.registry() as registry:
        loaded = registry.load(sid)
    assert loaded.spec.name == "trend"

    with pytest.raises(ConflictError):
        studio.register_draft(draft.id)

    assert studio.enable(draft.id).strategy_status == "active"
    assert svc.strategies.get(sid).status == "active"
    assert studio.disable(draft.id).strategy_status == "shadow"

    # editing the draft later never changes the registered snapshot
    studio.update_draft(draft.id, DraftUpdate(spec=SMA))
    assert svc.strategies.get(sid).params["spec"]["name"] == "trend"


def test_register_invalid_spec_is_rejected(studio):
    draft = studio.create_draft(DraftCreate(name="bad", spec={"version": 1}))
    with pytest.raises(ValidationError):
        studio.register_draft(draft.id)
    assert studio.get_draft(draft.id).status == "draft"


# ---- code strategies: disabled (default) -------------------------------------


def test_code_drafts_are_forbidden_by_default(studio, settings):
    assert settings.api.allow_code_strategies is False
    with pytest.raises(CodeStrategiesDisabledError, match="allow_code_strategies"):
        studio.create_draft(DraftCreate(name="c", kind="code", source_code=GOOD_CODE))


def test_every_code_draft_operation_is_forbidden_when_disabled(studio, settings):
    settings.api.allow_code_strategies = True
    draft = studio.create_draft(DraftCreate(name="c", kind="code", source_code=GOOD_CODE))
    settings.api.allow_code_strategies = False
    calls = [
        lambda: studio.get_draft(draft.id),
        lambda: studio.update_draft(draft.id, DraftUpdate(name="x")),
        lambda: studio.delete_draft(draft.id),
        lambda: studio.validate_draft(draft.id, ValidateRequest()),
        lambda: studio.submit_backtest(draft.id, _bt()),
        lambda: studio.submit_lab_run(draft.id, _lab()),
        lambda: studio.register_draft(draft.id),
        lambda: studio.enable(draft.id),
        lambda: studio.disable(draft.id),
    ]
    for call in calls:
        with pytest.raises(CodeStrategiesDisabledError):
            call()
    # nothing was written or imported
    assert not user_strategies_dir(settings).exists()
    assert not [m for m in sys.modules if m.startswith(USER_MODULE_PREFIX)]
    # listing still shows it exists, without its source
    (item,) = studio.list_drafts(limit=10, offset=0).items
    assert item.kind == "code" and item.source_code is None


def test_queued_code_job_fails_if_disabled_before_it_runs(svc, studio, settings):
    job = svc.runner.submit(
        STUDIO_BACKTEST_JOB,
        {"module": f"{USER_MODULE_PREFIX}.x_1", "path": "x_1.py", "params": {}, "request": {}},
    )
    done = svc.jobs.wait(job.id, timeout=30)
    assert done.status == "failed"
    assert "allow_code_strategies" in done.error


# ---- code strategies: enabled -----------------------------------------------


def test_code_draft_validate_backtest_register(svc, studio, code_on):
    draft = studio.create_draft(
        DraftCreate(name="Up", kind="code", source_code=GOOD_CODE, spec={"edge": 0.02})
    )
    assert draft.source_code == GOOD_CODE
    result = studio.validate_draft(draft.id, ValidateRequest())
    assert result.valid, result.issues
    assert result.smoke.ok and result.smoke.signals > 0
    files = list(user_strategies_dir(code_on).glob("*.py"))
    assert len(files) == 1 and files[0].stem.startswith(draft.id)

    job = studio.submit_backtest(draft.id, _bt())
    assert job.kind == STUDIO_BACKTEST_JOB
    done = svc.jobs.wait(job.id, timeout=60)
    assert done.status == "succeeded", done.error
    assert done.result["final_return"] > 0

    registered = studio.register_draft(draft.id)
    sid = registered.registered_strategy_id
    detail = svc.strategies.get(sid)
    assert detail.class_path.startswith(f"{USER_MODULE_PREFIX}.")
    assert detail.class_path.endswith(":AlwaysUp")
    assert detail.params == {"edge": 0.02}
    with svc.context.registry() as registry:
        assert registry.load(sid).estimate_return("UP.US", date(2026, 1, 5), None) == 0.02

    # the loader hook resolves the class from disk in a fresh process
    for name in [m for m in sys.modules if m.startswith(USER_MODULE_PREFIX)]:
        del sys.modules[name]
    assert studio.user_strategy_class(detail.class_path).__name__ == "AlwaysUp"
    with pytest.raises(ValidationError):
        studio.user_strategy_class(f"{USER_MODULE_PREFIX}.nope_1:AlwaysUp")
    with pytest.raises(ValidationError):
        studio.user_strategy_class("json:loads")
    code_on.api.allow_code_strategies = False
    with pytest.raises(CodeStrategiesDisabledError):
        studio.user_strategy_class(detail.class_path)


def test_code_lab_run(svc, studio, code_on):
    draft = studio.create_draft(DraftCreate(name="Up", kind="code", source_code=GOOD_CODE))
    job = studio.submit_lab_run(draft.id, _lab())
    done = svc.jobs.wait(job.id, timeout=120)
    assert done.status == "succeeded", done.error
    assert done.result["class_path"].endswith(":AlwaysUp")


@pytest.mark.parametrize(
    ("source", "needle"),
    [
        ("def broken(:\n", "SyntaxError"),
        ("x = 1\n", "exactly one BaseStrategy subclass"),
        (
            GOOD_CODE + "\n\nclass Other(AlwaysUp):\n    id = 'other'\n",
            "exactly one BaseStrategy subclass",
        ),
        (
            GOOD_CODE.replace('return float(self.params["edge"])', "raise RuntimeError('boom')"),
            "boom",
        ),
        (GOOD_CODE.replace("default=0.01", "default=5.0"), "edge"),
    ],
)
def test_code_smoke_check_failures(studio, code_on, source, needle):
    draft = studio.create_draft(DraftCreate(name="bad", kind="code", source_code=source))
    result = studio.validate_draft(draft.id, ValidateRequest())
    assert not result.valid
    assert any(needle in i.message for i in result.issues), result.issues
    with pytest.raises(ValidationError):
        studio.submit_backtest(draft.id, _bt())
    with pytest.raises(ValidationError):
        studio.register_draft(draft.id)


def test_user_code_is_never_imported_at_startup(settings, seeded, code_on, tmp_path):
    marker = tmp_path / "imported.txt"
    source = f"open({str(marker)!r}, 'w').write('x')\n" + GOOD_CODE
    ctx = AppContext(settings)
    first = Services.create(ctx)
    first.start()
    try:
        studio = studio_service(first)
        draft = studio.create_draft(DraftCreate(name="side", kind="code", source_code=source))
        studio.validate_draft(draft.id, ValidateRequest())
        assert marker.exists()
    finally:
        first.shutdown()
    marker.unlink()
    for name in [m for m in sys.modules if m.startswith(USER_MODULE_PREFIX)]:
        del sys.modules[name]
    second = Services.create(AppContext(settings))
    second.start()
    try:
        studio_service(second).list_drafts(limit=10, offset=0)
        assert not marker.exists()
    finally:
        second.shutdown()


def test_draft_names_are_validated():
    with pytest.raises(ValueError):
        DraftCreate(name="", spec=TREND)
    with pytest.raises(ValueError):
        DraftCreate(name="x" * 101, spec=TREND)


def test_spec_copy_isolated(studio):
    spec = copy.deepcopy(TREND)
    draft = studio.create_draft(DraftCreate(name="t", spec=spec))
    spec["name"] = "mutated"
    assert studio.get_draft(draft.id).spec["name"] == "trend"
