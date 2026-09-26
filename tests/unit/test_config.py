"""Unit tests for the Settings loader."""

from stonks.config import Settings, load_settings


def test_load_settings_uses_defaults_from_toml(tmp_path, monkeypatch):
    monkeypatch.delenv("EODHD_API_KEY", raising=False)
    monkeypatch.delenv("STONKS_DATA_DIR", raising=False)

    cfg = tmp_path / "cfg.toml"
    cfg.write_text(
        """
[lake]
path = "data/lake.duckdb"

[sources.eodhd]
base_url = "https://example.test/api"
timeout_seconds = 10
max_retries = 2
retry_backoff_seconds = 0.5

[logging]
level = "INFO"
""".strip()
    )

    settings = load_settings(config_path=cfg)
    assert isinstance(settings, Settings)
    assert settings.sources.eodhd.base_url == "https://example.test/api"
    assert settings.sources.eodhd.timeout_seconds == 10
    assert settings.lake.path.as_posix().endswith("data/lake.duckdb")


def test_env_overrides_toml_for_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("EODHD_API_KEY", "env-key-123")

    cfg = tmp_path / "cfg.toml"
    cfg.write_text(
        """
[sources.eodhd]
base_url = "https://example.test/api"
""".strip()
    )

    settings = load_settings(config_path=cfg)
    assert settings.sources.eodhd.api_key.get_secret_value() == "env-key-123"


def test_missing_api_key_is_none_not_error(tmp_path, monkeypatch):
    monkeypatch.delenv("EODHD_API_KEY", raising=False)
    cfg = tmp_path / "cfg.toml"
    cfg.write_text('[sources.eodhd]\nbase_url = "https://example.test/api"\n')
    settings = load_settings(config_path=cfg)
    assert settings.sources.eodhd.api_key is None


def test_production_max_price_staleness_days_reads_from_toml(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg.toml"
    cfg.write_text(
        """
[production]
max_price_staleness_days = 3
""".strip()
    )

    settings = load_settings(config_path=cfg)
    assert settings.production.max_price_staleness_days == 3


def test_production_risk_and_health_read_from_toml(tmp_path):
    cfg = tmp_path / "cfg.toml"
    cfg.write_text(
        """
[production]
shadow_enabled = false

[production.risk]
max_open_positions = 5
max_weight_per_ticker = 0.2
cash_buffer_fraction = 0.05
min_order_notional = 25.0

[production.risk.max_weight_per_asset_class]
crypto = 0.1

[production.health]
max_bar_age_days = 3
stuck_tick_minutes = 30
""".strip()
    )
    s = load_settings(config_path=cfg)
    assert s.production.shadow_enabled is False
    assert s.production.risk.max_open_positions == 5
    assert s.production.risk.max_weight_per_ticker == 0.2
    assert s.production.risk.max_weight_per_asset_class == {"crypto": 0.1}
    assert s.production.risk.cash_buffer_fraction == 0.05
    assert s.production.risk.min_order_notional == 25.0
    assert s.production.health.max_bar_age_days == 3
    assert s.production.health.stuck_tick_minutes == 30


def test_production_risk_defaults_are_permissive(tmp_path):
    cfg = tmp_path / "cfg.toml"
    cfg.write_text("")
    risk = load_settings(config_path=cfg).production.risk
    assert risk.enabled is True
    assert risk.max_open_positions is None
    assert risk.max_weight_per_ticker == 1.0
    assert risk.max_weight_per_asset_class == {}
    assert risk.cash_buffer_fraction == 0.0
    assert risk.min_order_notional == 0.0


def test_notify_section_reads_from_toml(tmp_path, monkeypatch):
    monkeypatch.delenv("STONKS_NOTIFY_WEBHOOK_URL", raising=False)
    cfg = tmp_path / "cfg.toml"
    cfg.write_text(
        """
[notify]
backends = ["log", "webhook"]
min_level = "error"

[notify.webhook]
url = "https://hooks.example.test/abc"
timeout_seconds = 2.5
""".strip()
    )
    s = load_settings(config_path=cfg)
    assert s.notify.backends == ["log", "webhook"]
    assert s.notify.min_level == "error"
    assert s.notify.webhook.url == "https://hooks.example.test/abc"
    assert s.notify.webhook.timeout_seconds == 2.5


def test_notify_webhook_url_env_overrides_toml(tmp_path, monkeypatch):
    monkeypatch.setenv("STONKS_NOTIFY_WEBHOOK_URL", "https://hooks.example.test/secret")
    cfg = tmp_path / "cfg.toml"
    cfg.write_text('[notify.webhook]\nurl = "https://toml.example.test/x"\n')
    s = load_settings(config_path=cfg)
    assert s.notify.webhook.url == "https://hooks.example.test/secret"


def test_notify_defaults_to_log_and_store_backends(tmp_path, monkeypatch):
    monkeypatch.delenv("STONKS_NOTIFY_WEBHOOK_URL", raising=False)
    cfg = tmp_path / "cfg.toml"
    cfg.write_text("")
    s = load_settings(config_path=cfg)
    assert s.notify.backends == ["log", "store"]
    assert s.notify.min_level == "warning"
    assert s.notify.webhook.url is None


def test_default_toml_parses(monkeypatch):
    from pathlib import Path

    monkeypatch.delenv("STONKS_NOTIFY_WEBHOOK_URL", raising=False)
    repo_cfg = Path(__file__).parents[2] / "config" / "default.toml"
    s = load_settings(config_path=repo_cfg)
    assert s.production.risk.enabled is True
    assert s.production.health.max_bar_age_days == 4
    assert s.notify.backends == ["log", "store"]
    assert s.notify.min_level == "warning"


def test_lab_benchmark_defaults_to_auto_in_code_and_toml(monkeypatch):
    from pathlib import Path

    from stonks.config import Settings

    monkeypatch.delenv("STONKS_NOTIFY_WEBHOOK_URL", raising=False)
    assert Settings().lab.benchmark == "auto"
    assert Settings().lab.embargo_bars == 0
    repo_cfg = Path(__file__).parents[2] / "config" / "default.toml"
    lab = load_settings(config_path=repo_cfg).lab
    assert lab.benchmark == "auto" and lab.embargo_bars == 0
    assert lab.walk_forward.min_wfe == 0.5 and lab.walk_forward.matrix is False


def test_default_toml_golive_matches_the_code_defaults(monkeypatch):
    from pathlib import Path

    from stonks.config import GoLivePolicy

    monkeypatch.delenv("STONKS_NOTIFY_WEBHOOK_URL", raising=False)
    repo_cfg = Path(__file__).parents[2] / "config" / "default.toml"
    assert load_settings(config_path=repo_cfg).golive == GoLivePolicy()


def test_breaker_halt_and_quit_rule_read_from_toml(tmp_path):
    cfg = tmp_path / "cfg.toml"
    cfg.write_text(
        """
[production.risk.rules.circuit_breaker]
max_month_loss = 0.06
max_week_loss = 0.04
max_drawdown_halt = 0.20

[production.risk.rules.operational_halt]
max_bar_age_days = 5

[production.quit_rule]
quit_multiple = 2.0
auto_demote = true
min_eval_days = 63
""".strip()
    )
    s = load_settings(config_path=cfg)
    breaker = s.production.risk.rules.circuit_breaker
    assert (breaker.max_month_loss, breaker.max_week_loss, breaker.max_drawdown_halt) == (
        0.06,
        0.04,
        0.20,
    )
    assert s.production.risk.rules.operational_halt.max_bar_age_days == 5
    q = s.production.quit_rule
    assert (q.quit_multiple, q.auto_demote, q.min_eval_days) == (2.0, True, 63)


def test_the_default_config_keeps_the_breaker_off_and_the_quit_rule_alerting():
    from pathlib import Path

    from stonks.production.quit_rule import QuitRuleSettings

    s = load_settings(config_path=Path("config/default.toml"))
    assert not s.production.risk.rules.circuit_breaker.active
    assert not s.production.risk.rules.operational_halt.active
    assert s.production.quit_rule == QuitRuleSettings(
        quit_multiple=1.5, auto_demote=False, min_eval_days=126
    )


def test_lab_preflight_and_audit_tolerances_read_from_toml(tmp_path):
    from stonks.store.audit import AuditTolerances

    cfg = tmp_path / "cfg.toml"
    cfg.write_text(
        """
[lab]
preflight = false
strict_preflight = true

[audit]
balance = 0.01
quarterly_sum = 0.10
""".strip()
    )
    s = load_settings(config_path=cfg)
    assert (s.lab.preflight, s.lab.strict_preflight) == (False, True)
    assert s.audit.tolerances() == AuditTolerances(balance=0.01, quarterly_sum=0.10)


def test_lab_preflight_and_audit_defaults(tmp_path):
    from stonks.store.audit import AuditTolerances

    cfg = tmp_path / "cfg.toml"
    cfg.write_text("")
    s = load_settings(config_path=cfg)
    assert (s.lab.preflight, s.lab.strict_preflight) == (True, False)
    assert s.audit.tolerances() == AuditTolerances()


def test_ensure_section_defaults_to_the_eodhd_free_plan(tmp_path):
    from stonks.ingest.ensure import EnsureSettings

    settings = load_settings(config_path=tmp_path / "missing.toml")
    assert isinstance(settings.ensure, EnsureSettings)
    assert settings.ensure.plans == {"eodhd": "free"}


def test_ensure_section_reads_from_toml(tmp_path):
    cfg = tmp_path / "cfg.toml"
    cfg.write_text('[ensure]\nmax_workers = 3\nplans = { eodhd = "all_world" }\n')
    settings = load_settings(config_path=cfg)
    assert settings.ensure.max_workers == 3
    assert settings.ensure.plans == {"eodhd": "all_world"}


def test_default_toml_has_an_ensure_section():
    import tomllib
    from pathlib import Path

    data = tomllib.loads(Path("config/default.toml").read_text(encoding="utf-8"))
    assert data["ensure"]["plans"] == {"eodhd": "free"}
    assert load_settings(Path("config/default.toml")).ensure.plans == {"eodhd": "free"}


def test_production_universe_accepts_a_list_or_a_universe_id(tmp_path):
    import pytest
    from pydantic import ValidationError

    cfg = tmp_path / "cfg.toml"
    cfg.write_text('[production]\nuniverse = ["A.US", "B.US"]\n')
    assert load_settings(config_path=cfg).production.universe == ["A.US", "B.US"]
    cfg.write_text('[production]\nuniverse = "sp500"\n')
    assert load_settings(config_path=cfg).production.universe == "sp500"
    cfg.write_text('[production]\nuniverse = "Not An Id"\n')
    with pytest.raises(ValidationError):
        load_settings(config_path=cfg)


def test_config_universe_id_pattern_matches_the_universes_block():
    from stonks import config
    from stonks.universes.base import UNIVERSE_ID_PATTERN

    assert config.UNIVERSE_ID_PATTERN == UNIVERSE_ID_PATTERN
