"""The shipped config is found from any working folder, and a missing
config file is loud (review wave 2, item 1). Without config/default.toml the
code defaults apply, and they are looser (one ticker may take the whole
book, the breaker is off)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from stonks.config import (
    DEFAULT_CONFIG_PATH,
    ConfigFileMissing,
    load_settings,
    resolve_default_config_path,
)

_PROJECT_CONFIG = Path(__file__).resolve().parents[2] / "config" / "default.toml"


def test_default_path_does_not_depend_on_the_working_folder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = resolve_default_config_path()
    assert path.is_absolute()
    assert path == _PROJECT_CONFIG


def test_a_config_in_the_working_folder_still_wins(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    local = tmp_path / "config" / "default.toml"
    local.write_text("[production]\nthreshold = 0.5\n")
    assert resolve_default_config_path() == local.resolve()
    assert load_settings().production.threshold == 0.5


def test_load_settings_from_another_folder_keeps_the_shipped_risk_limits(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = load_settings()
    assert settings.production.risk.max_weight_per_ticker == 0.25
    assert settings.production.risk.rules.circuit_breaker.active


def test_a_missing_config_file_logs_a_warning(tmp_path, caplog):
    missing = tmp_path / "nope.toml"
    with caplog.at_level(logging.WARNING, logger="stonks.config"):
        load_settings(missing)
    assert any("config file not found" in r.getMessage() for r in caplog.records)


def test_a_missing_config_file_fails_when_required(tmp_path):
    with pytest.raises(ConfigFileMissing):
        load_settings(tmp_path / "nope.toml", required=True)


def test_required_passes_with_the_shipped_file():
    assert load_settings(Path(DEFAULT_CONFIG_PATH), required=True).production.risk.enabled


@pytest.fixture
def no_shipped_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("stonks.config._PROJECT_CONFIG_PATH", tmp_path / "missing.toml")


def test_tick_refuses_to_run_without_the_config_file(no_shipped_config):
    from typer.testing import CliRunner

    from stonks.cli import app

    result = CliRunner().invoke(app, ["tick", "--dry-run", "--tickers", "AAPL.US"])
    assert result.exit_code != 0
    assert "config file not found" in result.output


def test_serve_refuses_to_run_without_the_config_file(no_shipped_config, monkeypatch):
    import uvicorn
    from typer.testing import CliRunner

    from stonks.cli import app

    started: list[object] = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: started.append(a))

    result = CliRunner().invoke(app, ["serve"])
    assert not started
    assert result.exit_code != 0
    assert "config file not found" in result.output


def test_app_factory_refuses_to_run_without_the_config_file(no_shipped_config):
    from stonks.api.server import app_factory

    with pytest.raises(ConfigFileMissing):
        app_factory()


def test_scheduler_refuses_to_run_without_the_config_file(no_shipped_config):
    from stonks.scheduling.__main__ import _load

    with pytest.raises(ConfigFileMissing):
        _load(None)
