"""Settings loader: merges TOML config + environment variables.

Precedence (low → high): defaults → TOML file → environment variables.
Only a handful of well-known env vars are mapped explicitly (API keys, paths),
so the config surface stays small and discoverable.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from stonks.backtest.costs import CostModelSettings
from stonks.core.types import AssetClass
from stonks.lab.survival.walk_forward import WalkForwardConfig

DEFAULT_CONFIG_PATH = Path("config/default.toml")


class EodhdSourceConfig(BaseModel):
    base_url: str = "https://eodhd.com/api"
    timeout_seconds: int = 30
    max_retries: int = 3
    retry_backoff_seconds: float = 1.0
    api_key: str | None = None


class YahooSourceConfig(BaseModel):
    timeout_seconds: int = 30
    max_retries: int = 3
    retry_backoff_seconds: float = 2.0
    # Yahoo rate-limits aggressively; space consecutive requests out.
    min_request_interval_seconds: float = 0.5


class SourcesConfig(BaseModel):
    eodhd: EodhdSourceConfig = EodhdSourceConfig()
    yahoo: YahooSourceConfig = YahooSourceConfig()


class AlpacaBrokerConfig(BaseModel):
    """Alpaca trading API. Paper by default; the live endpoint additionally
    requires ``allow_live = true``. Keys come from ``ALPACA_API_KEY`` /
    ``ALPACA_SECRET_KEY`` (env wins over TOML) and are kept as SecretStr so
    they never show up in reprs or logs."""

    paper: bool = True
    allow_live: bool = False
    max_retries: int = 3
    retry_backoff_seconds: float = 1.0
    api_key: SecretStr | None = None
    secret_key: SecretStr | None = None

    @model_validator(mode="after")
    def _keys_from_env(self) -> AlpacaBrokerConfig:
        api_key = os.environ.get("ALPACA_API_KEY")
        secret_key = os.environ.get("ALPACA_SECRET_KEY")
        if api_key:
            self.api_key = SecretStr(api_key)
        if secret_key:
            self.secret_key = SecretStr(secret_key)
        return self


class BrokersConfig(BaseModel):
    # Which broker the production tick trades through.
    kind: Literal["simulated", "alpaca"] = "simulated"
    alpaca: AlpacaBrokerConfig = Field(default_factory=AlpacaBrokerConfig)


class LakeConfig(BaseModel):
    path: Path = Path("data/lake.duckdb")


class StateConfig(BaseModel):
    path: Path = Path("data/state.sqlite")


class RegistryConfig(BaseModel):
    artifacts_dir: Path = Path("data/artifacts")


class LoggingConfig(BaseModel):
    level: str = "INFO"


class RiskPolicy(BaseModel):
    """Portfolio construction limits applied between ``strategy.decide`` and
    the broker (``[production.risk]``). Defaults are permissive, so an
    unconfigured install trades exactly what the strategy asks for.

    Weights are fractions of total portfolio value (cash + marked positions)
    before the tick's orders. Sells are never blocked, only clipped to the
    held quantity so they cannot open a short.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    # None = unlimited. Counts distinct tickers held after the tick's orders.
    max_open_positions: int | None = Field(default=None, ge=0)
    max_weight_per_ticker: float = Field(default=1.0, ge=0.0, le=1.0)
    # e.g. {"crypto": 0.2}. Classes not listed are uncapped; when any cap is
    # set, buys of tickers whose class is unknown are blocked.
    max_weight_per_asset_class: dict[AssetClass, Annotated[float, Field(ge=0.0, le=1.0)]] = {}
    # Fraction of portfolio value that must stay in cash after buys.
    cash_buffer_fraction: float = Field(default=0.0, ge=0.0, le=1.0)
    # Buys whose (possibly clipped) notional falls below this are dropped.
    min_order_notional: float = Field(default=0.0, ge=0.0)


class HealthConfig(BaseModel):
    """Thresholds for ``stonks health`` (``[production.health]``)."""

    model_config = ConfigDict(extra="forbid")

    # Latest daily bar older than this many calendar days is stale. 4 covers
    # a Monday check against Friday's close plus one day of vendor lag.
    max_bar_age_days: int = Field(default=4, ge=0)
    stuck_tick_minutes: int = Field(default=60, ge=1)
    stuck_ingest_minutes: int = Field(default=180, ge=1)
    # Ingest runs with status 'error' started within this window are unhealthy.
    ingest_failure_lookback_hours: int = Field(default=24, ge=1)


class ProductionConfig(BaseModel):
    universe: list[str] = []
    threshold: float = 0.0
    initial_cash: float = 10_000.0
    slippage_bps: float = 0.0
    fee_per_trade: float = 0.0
    max_price_staleness_days: int = 7
    # Evaluate shadow strategies each tick against virtual portfolios.
    shadow_enabled: bool = True
    risk: RiskPolicy = RiskPolicy()
    health: HealthConfig = HealthConfig()


class GoLivePolicy(BaseModel):
    """Limits a paper-trading period must meet before ``stonks golive check``
    passes (``[golive]``). The gate only reports; promotion stays a human
    action. Every limit is strict about missing data: a period with no
    snapshots, no fills or no backtest expectation fails."""

    model_config = ConfigDict(extra="forbid")

    # Distinct days with a paper snapshot.
    min_days: int = Field(default=20, ge=1)
    # Deepest peak-to-trough fall allowed, as a positive fraction (0.15 = -15%).
    max_drawdown: float = Field(default=0.15, gt=0.0, le=1.0)
    # Largest allowed |paper return - backtest-expected return| over the
    # period; the expectation compounds the ``oos`` survival report's CAGR.
    max_drift: float = Field(default=0.10, ge=0.0)
    # Filled trades during the paper period.
    min_trades: int = Field(default=5, ge=1)
    # Every stored survival report must have passed (and there must be one).
    require_all_survival_passed: bool = True


class WebhookConfig(BaseModel):
    # Secret-bearing (Slack/Discord URLs embed a token): prefer the
    # STONKS_NOTIFY_WEBHOOK_URL env var over committing it to TOML.
    url: str | None = None
    timeout_seconds: float = Field(default=5.0, gt=0)


class NotifyConfig(BaseModel):
    """Alert routing (``[notify]``)."""

    model_config = ConfigDict(extra="forbid")

    # Any of "log", "webhook". Empty list disables notifications.
    backends: list[Literal["log", "webhook"]] = ["log"]
    # Notifications below this level are dropped.
    min_level: Literal["info", "warning", "error"] = "warning"
    webhook: WebhookConfig = WebhookConfig()


def _api_token_from_env() -> SecretStr | None:
    token = os.environ.get("STONKS_API_TOKEN")
    return SecretStr(token) if token else None


class ApiConfig(BaseModel):
    """REST API server (``stonks serve``). The bearer token is env-only
    (``STONKS_API_TOKEN``) so it can never land in a checked-in TOML file."""

    host: str = "127.0.0.1"
    port: int = 8000
    # The only browser origin CORS lets through (the Angular dev server).
    ui_origin: str = "http://localhost:4200"
    # GET routes skip the token when the peer is a loopback address.
    open_reads_on_loopback: bool = True
    # Extra Host header values accepted besides localhost / 127.0.0.1 / ::1.
    allowed_hosts: list[str] = []
    max_concurrent_jobs: int = Field(default=2, ge=1)
    default_page_size: int = Field(default=50, ge=1)
    max_page_size: int = Field(default=500, ge=1)
    ui_dist: Path = Path("web/dist")
    token: SecretStr | None = Field(default_factory=_api_token_from_env)

    @model_validator(mode="before")
    @classmethod
    def _reject_file_token(cls, data: Any) -> Any:
        if isinstance(data, dict) and "token" in data:
            raise ValueError("api.token must not be set in config; use STONKS_API_TOKEN")
        return data


class McpConfig(BaseModel):
    """MCP server (``stonks mcp``): a client of the running REST API, never
    of the lake. The bearer token comes from ``STONKS_API_TOKEN`` only."""

    model_config = ConfigDict(extra="forbid")

    api_url: str = "http://127.0.0.1:8000"
    timeout_seconds: float = Field(default=30.0, gt=0)
    # Upper bound for the wait_for_job tool's timeout argument.
    max_wait_seconds: float = Field(default=600.0, gt=0)


class BacktestSettings(BaseModel):
    """``[backtest]``. ``costs`` (``[backtest.costs]``) is the transaction
    cost model for lab backtests; zero costs unless configured. Production
    can build the same model with ``settings.backtest.costs.build()``."""

    model_config = ConfigDict(extra="forbid")

    costs: CostModelSettings = CostModelSettings()


class LabSettings(BaseModel):
    """``[lab]``: defaults for ``stonks lab run``."""

    model_config = ConfigDict(extra="forbid")

    walk_forward: WalkForwardConfig = WalkForwardConfig()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    lake: LakeConfig = LakeConfig()
    state: StateConfig = StateConfig()
    registry: RegistryConfig = RegistryConfig()
    logging: LoggingConfig = LoggingConfig()
    brokers: BrokersConfig = Field(default_factory=BrokersConfig)
    sources: SourcesConfig = SourcesConfig()
    production: ProductionConfig = ProductionConfig()
    notify: NotifyConfig = NotifyConfig()
    api: ApiConfig = Field(default_factory=ApiConfig)
    backtest: BacktestSettings = BacktestSettings()
    lab: LabSettings = LabSettings()
    golive: GoLivePolicy = GoLivePolicy()
    mcp: McpConfig = McpConfig()


def load_settings(config_path: Path | None = None) -> Settings:
    """Load settings from TOML + environment (env wins for mapped keys).

    This reads from the *current* process environment only. Callers that
    want ``.env`` loaded first (CLI entry points, live tests) should do
    that themselves before calling this — keeps ``load_settings`` pure
    and test-monkeypatchable.
    """
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

    webhook_url = os.environ.get("STONKS_NOTIFY_WEBHOOK_URL")
    if webhook_url:
        data.setdefault("notify", {}).setdefault("webhook", {})["url"] = webhook_url

    log_level = os.environ.get("STONKS_LOG_LEVEL")
    if log_level:
        data.setdefault("logging", {})["level"] = log_level

    data_dir = os.environ.get("STONKS_DATA_DIR")
    if data_dir:
        # env > TOML for all three store paths (documented precedence).
        base = Path(data_dir)
        data.setdefault("lake", {})["path"] = str(base / "lake.duckdb")
        data.setdefault("state", {})["path"] = str(base / "state.sqlite")
        data.setdefault("registry", {})["artifacts_dir"] = str(base / "artifacts")
