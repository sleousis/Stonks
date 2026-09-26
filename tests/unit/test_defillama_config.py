"""``[sources.defillama]`` config sub-model + registry wiring."""

from __future__ import annotations

from pathlib import Path

from stonks.config import DefiLlamaSourceConfig, SourcesConfig, load_settings
from stonks.ingest.sources.defillama import DefiLlamaDataSource
from stonks.ingest.sources.registry import build_source


def test_defaults():
    cfg = DefiLlamaSourceConfig()
    assert cfg.base_url == "https://api.llama.fi"
    assert cfg.max_retries >= 1
    assert cfg.timeout_seconds > 0


def test_reads_from_toml(tmp_path, monkeypatch):
    monkeypatch.delenv("EODHD_API_KEY", raising=False)
    cfg = tmp_path / "c.toml"
    cfg.write_text('[sources.defillama]\nbase_url = "https://x.test"\nmax_retries = 5\n')
    settings = load_settings(cfg)
    assert settings.sources.defillama.base_url == "https://x.test"
    assert settings.sources.defillama.max_retries == 5


def test_default_toml_declares_defillama_section():
    root = Path(__file__).resolve().parents[2]
    settings = load_settings(root / "config" / "default.toml")
    assert settings.sources.defillama == DefiLlamaSourceConfig()


def test_build_defillama_needs_no_key():
    cfg = SourcesConfig.model_validate({"defillama": {"timeout_seconds": 5}})
    source = build_source("defillama", cfg)
    assert isinstance(source, DefiLlamaDataSource)
    assert source.source_id == "defillama"
    assert source._timeout == 5
