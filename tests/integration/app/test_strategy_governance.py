"""BL-24 / BL-26 through the service layer and the REST API: promotion is
gated by the go-live check, every change is audited, and strategy views
carry metadata and history."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from stonks.api import create_app
from stonks.app.errors import ConflictError, NotFoundError, ValidationError
from stonks.app.strategies import change_status
from stonks.config import GoLivePolicy
from stonks.core.protocols import SurvivalReport
from stonks.registry.store import StrategyRegistry
from stonks.store.state import SqliteState
from stonks.strategies.examples.buy_and_hold import BuyAndHold
from tests.integration.app.test_api import AUTH, LOOPBACK
from tests.paper_seed import TICK_ID, days, oos, seed_shadow

LONG_REASON = "owner override: incubation cut short, see ticket 42"


@pytest.fixture
def client(settings, seeded, fake_source):
    settings.api.allowed_hosts = ["testserver"]
    app = create_app(settings, source_factory=lambda: fake_source)
    with TestClient(app, client=LOOPBACK) as c:
        yield c


@pytest.fixture
def ready_id(settings, seeded) -> str:
    """A shadow strategy whose paper period passes the legacy go-live gate
    (the incubation checks have their own tests)."""
    settings.golive = GoLivePolicy(incubation=False, min_days=20, min_trades=5)
    with SqliteState(settings.state.path) as state:
        state.execute(
            "INSERT INTO tick_runs (id, started_at, finished_at, status) VALUES (?, ?, ?, 'ok')",
            [TICK_ID, "2026-01-01T00:00:00+00:00", "2026-01-01T00:01:00+00:00"],
        )
        registry = StrategyRegistry(state=state, artifacts_dir=settings.registry.artifacts_dir)
        sid = registry.register(
            BuyAndHold({"ticker": "UP.US", "allocation": 1.0}),
            reports=[oos(0.0)],
            strategy_id="bah_ready",
        )
        seed_shadow(state, sid, [(d, 10_000.0) for d in days(30)], fills=6)
    return sid


def _history(services, sid):
    return services.strategies.get(sid).status_history


# ---- promotion ---------------------------------------------------------------


def test_promote_without_golive_pass_is_a_conflict(services, seeded):
    sid = seeded["shadow_id"]
    with pytest.raises(ConflictError, match="go-live"):
        services.strategies.promote(sid)
    assert services.strategies.get(sid).status == "shadow"
    assert _history(services, sid) == []


def test_promote_with_passing_golive_needs_no_reason(services, ready_id):
    detail = services.strategies.promote(ready_id)
    assert detail.status == "active"
    (change,) = detail.status_history
    assert (change.from_status, change.to_status) == ("shadow", "active")
    assert change.golive_passed is True
    assert change.override is False
    assert change.actor == "api"


def test_override_needs_a_long_reason(services, seeded):
    sid = seeded["shadow_id"]
    for reason in (None, "because"):
        with pytest.raises(ValidationError, match="20"):
            services.strategies.promote(sid, override=True, reason=reason)
    assert services.strategies.get(sid).status == "shadow"


def test_override_promotes_and_keeps_the_failing_report(services, seeded):
    sid = seeded["shadow_id"]
    detail = services.strategies.promote(sid, override=True, reason=LONG_REASON, actor="cli")
    assert detail.status == "active"
    (change,) = detail.status_history
    assert change.override is True
    assert change.golive_passed is False
    assert change.reason == LONG_REASON
    assert change.actor == "cli"
    assert change.golive_report is not None
    assert any(not c["passed"] for c in change.golive_report["checks"])


# ---- demotion ---------------------------------------------------------------


@pytest.mark.parametrize("action", ["retire", "shadow"])
def test_demotion_requires_a_reason(services, seeded, action):
    sid = seeded["active_id"]
    with pytest.raises(ValidationError, match="reason"):
        getattr(services.strategies, action)(sid)
    assert services.strategies.get(sid).status == "active"


@pytest.mark.parametrize(("action", "status"), [("retire", "retired"), ("shadow", "shadow")])
def test_demotion_with_reason_is_logged(services, seeded, action, status):
    sid = seeded["active_id"]
    detail = getattr(services.strategies, action)(sid, reason="edge decayed in live trading")
    assert detail.status == status
    change = detail.status_history[-1]
    assert (change.from_status, change.to_status) == ("active", status)
    assert change.reason == "edge decayed in live trading"


def test_unknown_id_is_not_found(services):
    for call in (
        lambda: services.strategies.promote("missing"),
        lambda: services.strategies.retire("missing", reason="gone"),
        lambda: services.strategies.history("missing"),
    ):
        with pytest.raises(NotFoundError):
            call()


def test_change_status_is_the_shared_entry_point(services, seeded):
    sid = seeded["active_id"]
    change_status(services.context, sid, "retired", actor="studio", reason="replaced by v2")
    assert services.strategies.history(sid)[-1].actor == "studio"


# ---- views ------------------------------------------------------------------


def test_views_carry_metadata(services, seeded):
    summary = services.strategies.list(limit=10, offset=0).items[0]
    assert summary.metadata.alpha_family == "benchmark"
    assert summary.metadata.premise == "none"
    assert summary.metadata.hypothesis == BuyAndHold.hypothesis
    detail = services.strategies.get(seeded["active_id"])
    assert detail.metadata.label_horizon_bars == 0


def test_promotion_warns_when_hypothesis_is_empty(services, seeded, monkeypatch):
    monkeypatch.setattr(BuyAndHold, "hypothesis", "")
    warnings = services.strategies.promotion_warnings(seeded["shadow_id"])
    assert any("hypothesis" in w for w in warnings)


def test_asset_classes_override_shows_in_views(services, settings, seeded):
    with SqliteState(settings.state.path) as state:
        registry = StrategyRegistry(state=state, artifacts_dir=settings.registry.artifacts_dir)
        sid = registry.register(
            BuyAndHold({"ticker": "BTC-USD.CC", "asset_classes": ["crypto"]}),
            reports=[SurvivalReport(test_id="oos", passed=True, metrics={})],
        )
    summary = services.strategies.get(sid)
    assert summary.applicable_asset_classes == ["crypto"]


# ---- the API uses the same rules --------------------------------------------


def test_api_promote_is_refused_without_golive(client, seeded):
    resp = client.post(f"/api/strategies/{seeded['shadow_id']}/promote", headers=AUTH)
    assert resp.status_code == 409
    assert "go-live" in resp.json()["detail"]
    detail = client.get(f"/api/strategies/{seeded['shadow_id']}").json()
    assert detail["status"] == "shadow"
    assert detail["status_history"] == []


def test_api_promote_passes_with_golive(client, ready_id):
    resp = client.post(f"/api/strategies/{ready_id}/promote", headers=AUTH)
    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert body["status"] == "active"
    assert body["status_history"][0]["golive_passed"] is True
    assert body["metadata"]["alpha_family"] == "benchmark"


@pytest.mark.parametrize("action", ["retire", "shadow"])
def test_api_demotion_without_reason_is_refused(client, seeded, action):
    resp = client.post(f"/api/strategies/{seeded['active_id']}/{action}", headers=AUTH)
    assert resp.status_code == 422
    assert "reason" in resp.json()["detail"]
