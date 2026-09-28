"""System settings an admin edits in the console (complexity audit F61):
a catalog of safe, non-secret keys, applied as validated overrides on top of
the TOML settings and stored in the state DB with an audit row."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from stonks.config import DEFAULT_CONFIG_PATH, LakeConfig, Settings, StateConfig, load_settings
from stonks.config_overrides import (
    NotEditable,
    OverrideStore,
    apply_override,
    apply_overrides,
    editable_settings,
    find_setting,
    read_value,
)
from stonks.store.state import SqliteState


@pytest.fixture
def base() -> Settings:
    return load_settings(DEFAULT_CONFIG_PATH)


def test_the_catalog_covers_risk_schedule_and_notification_defaults(base):
    keys = {s.key for s in editable_settings(base)}
    assert "production.risk.max_weight_per_ticker" in keys
    assert "production.risk.rules.circuit_breaker.max_month_loss" in keys
    assert "production.risk.rules.drawdown_scaling.schedule" in keys
    assert "production.universe" in keys
    assert "production.model_books" in keys
    assert "notify.min_level" in keys
    assert "scheduler.jobs.tick.enabled" in keys
    groups = {s.group for s in editable_settings(base)}
    assert groups == {"risk", "trading", "notifications", "schedule"}


def test_no_secret_or_store_path_is_editable(base):
    for key in (
        "sources.eodhd.api_key",
        "notify.webhook.url",
        "brokers.alpaca.secret_key",
        "api.token",
        "state.path",
        "lake.path",
        "auth.session_hours",
        "scheduler.jobs.tick.ping_url",
        "scheduler.api_url",
    ):
        assert find_setting(base, key) is None, key
        with pytest.raises(NotEditable):
            apply_override(base, key, "x")


def test_an_override_changes_only_its_key(base):
    out = apply_override(base, "production.risk.max_weight_per_ticker", 0.1)
    assert out.production.risk.max_weight_per_ticker == 0.1
    assert base.production.risk.max_weight_per_ticker == 0.25  # base untouched
    assert out.production.risk.rules == base.production.risk.rules
    assert out.lake == base.lake


def test_nested_rule_and_schedule_job_keys(base):
    out = apply_override(base, "production.risk.rules.circuit_breaker.max_month_loss", 0.05)
    assert out.production.risk.rules.circuit_breaker.max_month_loss == 0.05
    out = apply_override(base, "scheduler.jobs.tick.enabled", False)
    tick = next(j for j in out.scheduler.jobs if j.name == "tick")
    assert tick.enabled is False
    assert read_value(out, "scheduler.jobs.tick.enabled") is False


def test_invalid_values_are_refused(base):
    with pytest.raises(ValidationError):
        apply_override(base, "production.risk.max_weight_per_ticker", 1.5)
    with pytest.raises(ValidationError):
        apply_override(
            base, "production.risk.rules.drawdown_scaling.schedule", [[0.2, 0.5], [0.1, 0.0]]
        )
    with pytest.raises(ValidationError):
        apply_override(base, "notify.min_level", "loud")


def test_a_bad_stored_override_is_skipped_not_fatal(base):
    out, problems = apply_overrides(
        base,
        {"production.risk.max_weight_per_ticker": 7, "production.model_books": "shadow"},
    )
    assert out.production.risk.max_weight_per_ticker == 0.25
    assert out.production.model_books == "shadow"
    assert list(problems) == ["production.risk.max_weight_per_ticker"]


def test_the_store_keeps_overrides_and_audits_each_change(tmp_path):
    settings = Settings(
        lake=LakeConfig(path=tmp_path / "lake.duckdb"),
        state=StateConfig(path=tmp_path / "state.sqlite"),
    )
    with SqliteState(settings.state.path) as state:
        state.migrate()
        store = OverrideStore(state)
        store.set("production.model_books", "shadow", actor="user:ada", reason="save time")
        store.set("production.model_books", "all", actor="user:ada", reason="show records")
        assert store.values() == {"production.model_books": "all"}
        assert store.reset("production.model_books", actor="user:ada", reason="back to TOML")
        assert not store.reset("production.model_books", actor="user:ada", reason="again")
        assert store.values() == {}
        audit = state.sql(
            "SELECT action, target_id, details_json FROM audit_log"
            " WHERE target_kind = 'setting' ORDER BY id"
        )
    assert [r["action"] for r in audit] == [
        "settings.override",
        "settings.override",
        "settings.reset",
    ]
    assert all(r["target_id"] == "production.model_books" for r in audit)
    assert '"reason": "show records"' in audit[1]["details_json"]


def test_the_store_reads_nothing_before_the_table_exists(tmp_path):
    with SqliteState(tmp_path / "state.sqlite") as state:
        assert OverrideStore(state).values() == {}
