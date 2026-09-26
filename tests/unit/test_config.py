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
    assert settings.sources.eodhd.api_key == "env-key-123"


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
