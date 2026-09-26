"""Source selection: config sub-model + the ``build_source`` factory."""

from __future__ import annotations

from pathlib import Path

import pytest

from stonks.config import SourcesConfig, YahooSourceConfig, load_settings
from stonks.ingest.sources.eodhd import EodhdDataSource
from stonks.ingest.sources.registry import (
    SOURCE_IDS,
    SourceConfigError,
    build_source,
)
from stonks.ingest.sources.yahoo import YahooDataSource


def test_yahoo_config_defaults():
    cfg = YahooSourceConfig()
    assert cfg.max_retries >= 1
    assert cfg.min_request_interval_seconds >= 0


def test_yahoo_config_reads_from_toml(tmp_path, monkeypatch):
    monkeypatch.delenv("EODHD_API_KEY", raising=False)
    cfg = tmp_path / "c.toml"
    cfg.write_text("[sources.yahoo]\nmax_retries = 7\nmin_request_interval_seconds = 1.5\n")
    settings = load_settings(cfg)
    assert settings.sources.yahoo.max_retries == 7
    assert settings.sources.yahoo.min_request_interval_seconds == 1.5


def test_default_toml_declares_yahoo_section():
    root = Path(__file__).resolve().parents[2]
    settings = load_settings(root / "config" / "default.toml")
    assert settings.sources.yahoo == YahooSourceConfig()


def test_source_ids():
    assert SOURCE_IDS == ("eodhd", "yahoo", "defillama")


def test_build_eodhd_requires_api_key():
    with pytest.raises(SourceConfigError, match="EODHD_API_KEY"):
        build_source("eodhd", SourcesConfig())


def test_build_eodhd():
    cfg = SourcesConfig.model_validate({"eodhd": {"api_key": "k", "timeout_seconds": 5}})
    source = build_source("eodhd", cfg)
    assert isinstance(source, EodhdDataSource)
    assert source.source_id == "eodhd"


def test_build_yahoo_needs_no_key():
    source = build_source("yahoo", SourcesConfig())
    assert isinstance(source, YahooDataSource)
    assert source.source_id == "yahoo"


def test_unknown_source_lists_choices():
    with pytest.raises(SourceConfigError, match="eodhd"):
        build_source("bloomberg", SourcesConfig())
