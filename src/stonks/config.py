"""Settings loader: merges TOML config + environment variables.

Precedence (low → high): defaults → TOML file → environment variables.
Only a handful of well-known env vars are mapped explicitly (API keys, paths),
so the config surface stays small and discoverable.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_CONFIG_PATH = Path("config/default.toml")


class EodhdSourceConfig(BaseModel):
    base_url: str = "https://eodhd.com/api"
    timeout_seconds: int = 30
    max_retries: int = 3
    retry_backoff_seconds: float = 1.0
    api_key: str | None = None


class SourcesConfig(BaseModel):
    eodhd: EodhdSourceConfig = EodhdSourceConfig()


class LakeConfig(BaseModel):
    path: Path = Path("data/lake.duckdb")


class StateConfig(BaseModel):
    path: Path = Path("data/state.sqlite")


class RegistryConfig(BaseModel):
    artifacts_dir: Path = Path("data/artifacts")


class LoggingConfig(BaseModel):
    level: str = "INFO"


class ProductionConfig(BaseModel):
    universe: list[str] = []
    threshold: float = 0.0
    initial_cash: float = 10_000.0
    slippage_bps: float = 0.0
    fee_per_trade: float = 0.0


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    lake: LakeConfig = LakeConfig()
    state: StateConfig = StateConfig()
    registry: RegistryConfig = RegistryConfig()
    logging: LoggingConfig = LoggingConfig()
    sources: SourcesConfig = SourcesConfig()
    production: ProductionConfig = ProductionConfig()


def load_settings(config_path: Path | None = None) -> Settings:
    """Load settings from TOML + environment (env wins for mapped keys)."""
    load_dotenv(override=False)

    if config_path is None:
        config_path = DEFAULT_CONFIG_PATH

    data: dict = {}
    if config_path is not None and Path(config_path).exists():
        with open(config_path, "rb") as f:
            data = tomllib.load(f)

    _overlay_env(data)
    return Settings(**data)


def _overlay_env(data: dict) -> None:
    api_key = os.environ.get("EODHD_API_KEY")
    if api_key:
        data.setdefault("sources", {}).setdefault("eodhd", {})["api_key"] = api_key

    log_level = os.environ.get("STONKS_LOG_LEVEL")
    if log_level:
        data.setdefault("logging", {})["level"] = log_level

    data_dir = os.environ.get("STONKS_DATA_DIR")
    if data_dir:
        base = Path(data_dir)
        data.setdefault("lake", {}).setdefault("path", str(base / "lake.duckdb"))
        data.setdefault("state", {}).setdefault("path", str(base / "state.sqlite"))
        data.setdefault("registry", {}).setdefault("artifacts_dir", str(base / "artifacts"))
