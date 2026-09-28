"""Settings loader: merges TOML config + environment variables.

Precedence (low → high): defaults → TOML file → environment variables.
Only a handful of well-known env vars are mapped explicitly (API keys, paths),
so the config surface stays small and discoverable.
"""

from __future__ import annotations

import ipaddress
import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from stonks.assistant.settings import AssistantConfig
from stonks.backtest.costs import CostModelSettings
from stonks.backtest.fills import ExecutionSettings
from stonks.core.types import AssetClass
from stonks.engine.settings import EngineSettings
from stonks.execution.brokers.ibkr.settings import IbkrBrokerConfig
from stonks.factors.settings import FactorSettings
from stonks.ingest.ensure_settings import EnsureSettings
from stonks.ingest.quality_config import DataQualityConfig, FallbackConfig
from stonks.lab.offload.settings import LabOffloadSettings
from stonks.lab.parallel import ParallelSettings
from stonks.lab.survival.walk_forward import WalkForwardConfig
from stonks.lifecycle.settings import ModelLifecycleSettings
from stonks.ops.config import BackupConfig
from stonks.options.live.settings import OptionsLiveSettings
from stonks.portfolio.settings import ConstructionSettings
from stonks.production.decay import DecaySettings
from stonks.production.feature_drift_settings import FeatureDriftSettings
from stonks.production.intraday_pnl_settings import IntradayPnlSettings
from stonks.production.live.settings import LiveSettings
from stonks.production.monitor_settings import RiskMonitorSettings
from stonks.production.quit_rule import QuitRuleSettings
from stonks.production.rules.settings import RuleSettings
from stonks.scheduling.config import SchedulerConfig
from stonks.screener.settings import ScreenerSettings
from stonks.store.audit import AuditTolerances
from stonks.store.bars import BarBackend
from stonks.streaming.settings import StreamingSettings
from stonks.telegram.settings import TelegramConfig

DEFAULT_CONFIG_PATH = Path("config/default.toml")

#: Same as ``stonks.universes.base.UNIVERSE_ID_PATTERN`` (importing the
#: universes package here would be an import cycle; a test keeps them equal).
UNIVERSE_ID_PATTERN = r"^[a-z0-9][a-z0-9_.-]{0,63}$"


class EodhdSourceConfig(BaseModel):
    # A raw str assigned later (tests, scripts) still becomes a SecretStr.
    model_config = ConfigDict(validate_assignment=True)

    base_url: str = "https://eodhd.com/api"
    timeout_seconds: int = 30
    max_retries: int = 3
    retry_backoff_seconds: float = 1.0
    #: From ``EODHD_API_KEY``; a SecretStr so it never shows in a repr.
    api_key: SecretStr | None = None


class YahooSourceConfig(BaseModel):
    timeout_seconds: int = 30
    max_retries: int = 3
    retry_backoff_seconds: float = 2.0
    # Yahoo rate-limits aggressively; space consecutive requests out.
    min_request_interval_seconds: float = 0.5


class DefiLlamaSourceConfig(BaseModel):
    # Free public API, no key. Serves DeFi TVL only (``stonks ingest tvl``).
    base_url: str = "https://api.llama.fi"
    timeout_seconds: int = 30
    max_retries: int = 3
    retry_backoff_seconds: float = 1.0


class IbkrBorrowSourceConfig(BaseModel):
    """IBKR's public short stock files (``stonks ingest borrow``, roadmap
    19.3). The FTP login is IBKR's shared public one, not an account."""

    model_config = ConfigDict(extra="forbid")

    host: str = "ftp2.interactivebrokers.com"
    user: str = "shortstock"
    timeout_seconds: float = 30.0
    #: The markets ``stonks ingest borrow`` pulls when none are named.
    markets: tuple[str, ...] = ("usa",)


class SourcesConfig(BaseModel):
    eodhd: EodhdSourceConfig = EodhdSourceConfig()
    yahoo: YahooSourceConfig = YahooSourceConfig()
    defillama: DefiLlamaSourceConfig = DefiLlamaSourceConfig()
    ibkr_borrow: IbkrBorrowSourceConfig = IbkrBorrowSourceConfig()


def _env_secret(name: str) -> SecretStr | None:
    value = os.environ.get(name)
    return SecretStr(value) if value else None


class AlpacaBrokerConfig(BaseModel):
    """Alpaca trading API, used only when ``[brokers].kind = "alpaca"``.
    Paper by default; the live endpoint additionally requires
    ``allow_live = true``. Keys are env-only (``ALPACA_API_KEY`` /
    ``ALPACA_SECRET_KEY``), exactly like the API token, so they can never land
    in a checked-in TOML file, and are kept as SecretStr so they never show
    up in reprs or logs."""

    # A rejected TOML key must not be echoed back in the validation error.
    model_config = ConfigDict(hide_input_in_errors=True)

    paper: bool = True
    allow_live: bool = False
    #: Short sales through Alpaca (roadmap 16.1). Off by default.
    allow_short: bool = False
    max_retries: int = 3
    retry_backoff_seconds: float = 1.0
    api_key: SecretStr | None = Field(default_factory=lambda: _env_secret("ALPACA_API_KEY"))
    secret_key: SecretStr | None = Field(default_factory=lambda: _env_secret("ALPACA_SECRET_KEY"))

    @model_validator(mode="before")
    @classmethod
    def _reject_file_keys(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        found = [k for k in ("api_key", "secret_key") if k in data]
        if found:
            # Outer models render the offending input in their error message;
            # blank the secrets in place so it can't echo them.
            for key in found:
                data[key] = "**********"
            names = ", ".join(f"brokers.alpaca.{k}" for k in found)
            raise ValueError(
                f"{names} must not be set in config; use ALPACA_API_KEY / ALPACA_SECRET_KEY"
            )
        return data


class BrokersConfig(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    # Which broker the production tick trades through. "simulated" (default)
    # needs no keys; "alpaca" is opt-in and needs ALPACA_API_KEY/SECRET_KEY.
    # "ibkr" trades through the IB Gateway that lists the default portfolio
    # (``[brokers.ibkr]``, roadmap 19.2).
    kind: Literal["simulated", "alpaca", "ibkr"] = "simulated"
    alpaca: AlpacaBrokerConfig = Field(default_factory=AlpacaBrokerConfig)
    # ``[brokers.ibkr]``: the IB Gateways Stonks can reach (roadmap 19.4).
    ibkr: IbkrBrokerConfig = Field(default_factory=IbkrBrokerConfig)


class LakeBarsConfig(BaseModel):
    """Where the lake keeps its OHLCV bars (``[lake.bars]``, roadmap 10.4).

    ``duckdb``: the ``bars`` table in the lake file. ``parquet``:
    hive-partitioned files under ``<lake dir>/bars``, readable by other
    processes while ``stonks serve`` holds the lake. The lake records the
    store it uses, so changing this value alone switches nothing: run
    ``uv run python -m stonks.store.bars_migrate`` to move the bars and
    switch."""

    backend: BarBackend = "duckdb"


class LakeConfig(BaseModel):
    path: Path = Path("data/lake.duckdb")
    bars: LakeBarsConfig = LakeBarsConfig()


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
    # ``[production.risk.rules.<rule>]``: the W3.1 rules (max_holding,
    # drawdown_scaling, portfolio_vol, risk_per_position, sector_cap,
    # liquidity) and the W3.2 halts (circuit_breaker, operational_halt),
    # every one off by default.
    rules: RuleSettings = RuleSettings()

    def tighter_of(self, *overrides: RiskPolicy | Mapping[str, Any] | None) -> RiskPolicy:
        """This policy tightened by each partial override (a ``RiskPolicy``
        or a mapping of its fields): never looser on any field (P28). Same
        as ``stonks.accounts.book.tighter_of``; ``RiskPolicy.tighter_of(base,
        ...)`` works too."""
        from stonks.accounts.book import tighter_of

        return tighter_of(self, *overrides)


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
    # Lab worker jobs queued longer than this with no live worker are
    # unhealthy (roadmap 14.9). Never trips the operational halt.
    stuck_lab_queue_minutes: int = Field(default=30, ge=1)


class ProductionConfig(BaseModel):
    # A ticker list, or the id of a stored universe (roadmap 10.5) whose
    # members on the tick's date are traded (stonks.production.universe).
    universe: list[str] | Annotated[str, Field(pattern=UNIVERSE_ID_PATTERN)] = []
    threshold: float = 0.0
    initial_cash: float = 10_000.0
    slippage_bps: float = 0.0
    fee_per_trade: float = 0.0
    max_price_staleness_days: int = 7
    # Evaluate shadow strategies each tick against virtual portfolios.
    shadow_enabled: bool = True
    # Fraction of each cash dividend withheld as tax in the tick and shadow
    # books (0 = credited in full).
    dividend_withholding_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    risk: RiskPolicy = RiskPolicy()
    health: HealthConfig = HealthConfig()
    # ``[production.construction]``: the global constructor and no-trade
    # buffer (default ``single_winner``, today's behaviour); a portfolio's
    # ``construction_json`` is merged on top. A factory, so importing this
    # module does not run constructor discovery (which imports
    # ``portfolio.pipeline`` and, through it, ``production.risk``).
    construction: ConstructionSettings = Field(default_factory=ConstructionSettings)
    # Which strategies keep a model book: "shadow" (only shadow strategies)
    # or "all" non-retired ones (design section 5).
    model_books: Literal["shadow", "all"] = "shadow"
    # How paper books and model books fill (P21): "next_open" keeps an order
    # working and fills it at the next session's open through
    # [backtest.execution] and [backtest.costs], as a backtest does.
    # "close" fills at once at the latest close (the old convention).
    paper_fills: Literal["next_open", "close"] = "next_open"
    # Trade one book per portfolio from its paper/auto subscriptions (and
    # record notify signals). pf_default follows every active strategy: a
    # promotion subscribes it (accounts.default_book), so it trades like
    # the old single book. false: the old single book over every active
    # strategy, other portfolios idle.
    books_from_subscriptions: bool = True
    # Worker processes that score strategies opting in with
    # ``parallel_scoring`` (0 = every core, 1 = in the tick's process). A
    # pool starts only for at least ``parallel_min_estimates`` estimates.
    scoring_workers: int = Field(default=0, ge=0)
    parallel_min_estimates: int = Field(default=2000, ge=1)
    # ``[production.quit_rule]`` (BL-29): alert (and with auto_demote, move
    # to shadow) an active strategy whose attributed drawdown passes
    # quit_multiple x its backtest drawdown.
    quit_rule: QuitRuleSettings = QuitRuleSettings()
    # ``[production.risk_monitor]`` (BL-47): daily VaR and ES snapshots and
    # the violation checks after each real tick.
    risk_monitor: RiskMonitorSettings = RiskMonitorSettings()
    # ``[production.decay]``: the alpha-decay check per strategy sleeve.
    decay: DecaySettings = DecaySettings()
    # ``[production.feature_drift]`` (roadmap 23.10): PSI of model
    # strategies' live features against their training profile, warn only.
    feature_drift: FeatureDriftSettings = FeatureDriftSettings()
    # ``[production.live]``: live trading at a real broker (roadmap 19).
    live: LiveSettings = LiveSettings()
    # ``[production.options]``: live options at a real broker (roadmap
    # 17.8). Off by default.
    options: OptionsLiveSettings = OptionsLiveSettings()
    # ``[production.intraday_pnl]``: live marks and intraday P&L snapshots
    # of the engine's books (roadmap 21.3.3). Off by default.
    intraday_pnl: IntradayPnlSettings = IntradayPnlSettings()


class GoLivePolicy(BaseModel):
    """Limits a paper-trading period must meet before ``stonks golive check``
    passes (``[golive]``). The gate only reports; promotion stays a human
    action. Every limit is strict about missing data: a period with no
    snapshots, no fills or no backtest expectation fails."""

    model_config = ConfigDict(extra="forbid")

    # Distinct days with a paper snapshot (with ``incubation``, at least the
    # MinTRL of the oos Sharpe, capped at ``min_trl_cap_days``).
    min_days: int = Field(default=63, ge=1)
    # Deepest peak-to-trough fall allowed, as a positive fraction (0.15 = -15%).
    max_drawdown: float = Field(default=0.15, gt=0.0, le=1.0)
    # Largest allowed |paper return - backtest-expected return| over the
    # period; the expectation compounds the ``oos`` survival report's CAGR.
    max_drift: float = Field(default=0.10, ge=0.0)
    # Filled trades during the paper period.
    min_trades: int = Field(default=20, ge=1)
    # Every stored survival report must have passed (and there must be one).
    require_all_survival_passed: bool = True

    # ---- incubation grade (BL-25; ``stonks.production.golive``) ----
    # Adds within_mc_band, quit_rule, promotion_preset, nonzero_costs,
    # hypothesis_recorded and backtest_min_trades; false keeps the six
    # legacy checks.
    incubation: bool = True
    # Day requirement = max(min_days, MinTRL of the oos Sharpe).
    use_min_trl: bool = True
    min_trl_cap_days: int = Field(default=252, ge=1)
    # The track record must reach PSR >= 1 - alpha.
    min_trl_alpha: float = Field(default=0.05, gt=0.0, lt=1.0)
    # De-annualises the oos Sharpe to a per-bar Sharpe for MinTRL.
    periods_per_year: float = Field(default=252.0, gt=0.0)
    # Quit when the paper drawdown exceeds this multiple of the backtest's
    # (or the Monte Carlo 95th percentile, when tighter).
    quit_drawdown_multiple: float = Field(default=1.5, ge=1.0)
    # Survival preset whose registered tests all need a stored report.
    promotion_preset: str = "promotion"
    # Closed trades the backtest must have made (oos or mc_trades n_trades).
    min_backtest_trades: int = Field(default=30, ge=1)
    # Characters of recorded hypothesis (lab run or strategy class).
    min_hypothesis_chars: int = Field(default=20, ge=1)


class WebhookConfig(BaseModel):
    # Secret-bearing (Slack/Discord URLs embed a token): prefer the
    # STONKS_NOTIFY_WEBHOOK_URL env var over committing it to TOML.
    url: str | None = None
    timeout_seconds: float = Field(default=5.0, gt=0)


class NotifyConfig(BaseModel):
    """Alert routing (``[notify]``)."""

    model_config = ConfigDict(extra="forbid")

    # Any of "log", "webhook", "store", "outbox". Empty list disables
    # notifications. "store" persists every notification (all levels,
    # redacted) to the state DB's ``alerts`` table, which ``GET /api/alerts``
    # reads. "outbox" sends each one to every active admin through the
    # per-user outbox (Web Push, email, their webhook; see stonks.notify).
    backends: list[Literal["log", "webhook", "store", "outbox"]] = ["log", "store"]
    # Notifications below this level are dropped (except by "store").
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
    # GET routes skip the credential when the peer is a loopback address.
    # Off by default; the dev profile (STONKS_PROFILE=dev) turns it on.
    open_reads_on_loopback: bool = False
    # Peers whose X-Forwarded-For / X-Forwarded-Proto uvicorn believes
    # (`stonks serve` passes them as forwarded_allow_ips). IPs or CIDR
    # networks. Behind Caddy in Compose this is the Docker network, so the
    # login limit and the audit log see the real client IP. A forwarded
    # header from any other peer is ignored. Env: STONKS_API_TRUSTED_PROXIES
    # (comma-separated).
    trusted_proxies: list[str] = Field(default_factory=lambda: ["127.0.0.1"])
    # Extra Host header values accepted besides localhost / 127.0.0.1 / ::1
    # (and ``host`` when it names one address). Behind Caddy: the public
    # domain, plus ``api`` for the scheduler on the Compose network. A
    # ``*.example.com`` entry allows its subdomains; a bare ``*`` is refused.
    # Env: STONKS_API_ALLOWED_HOSTS (comma-separated). STONKS_API_HOST sets
    # ``host``.
    allowed_hosts: list[str] = []
    # Size of the general job pool (backtests, lab runs); ticks and ingests
    # each have one dedicated worker on top.
    max_concurrent_jobs: int = Field(default=2, ge=1)
    # A job event stream closes (event ``end``, reason ``timeout``) after this.
    sse_max_stream_seconds: float = Field(default=3600.0, gt=0)
    # Lifetime of a job-scoped ``?token=`` for event streams (EventSource
    # can't send the bearer header); see POST /api/jobs/{id}/stream-token.
    stream_token_ttl_seconds: int = Field(default=300, ge=10, le=3600)
    default_page_size: int = Field(default=50, ge=1)
    max_page_size: int = Field(default=500, ge=1)
    ui_dist: Path = Path("web/dist")
    token: SecretStr | None = Field(default_factory=_api_token_from_env)
    # Strategy Studio: allow saving and running user Python strategies
    # (arbitrary code with the server's privileges). Off by default.
    allow_code_strategies: bool = False

    @model_validator(mode="before")
    @classmethod
    def _reject_file_token(cls, data: Any) -> Any:
        if isinstance(data, dict) and "token" in data:
            raise ValueError("api.token must not be set in config; use STONKS_API_TOKEN")
        return data

    @field_validator("allowed_hosts")
    @classmethod
    def _hosts_are_names(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        for raw in value:
            item = raw.strip().lower()
            if not item:
                continue
            if item == "*" or "*" in item.removeprefix("*."):
                raise ValueError(f"api.allowed_hosts: {raw!r} would accept any host; name each one")
            if any(ch in item for ch in "/@ ?#") or "://" in item:
                raise ValueError(f"api.allowed_hosts: {raw!r} is not a host name")
            out.append(item)
        return out

    @field_validator("trusted_proxies")
    @classmethod
    def _proxies_are_networks(cls, value: list[str]) -> list[str]:
        for item in value:
            try:
                ipaddress.ip_network(item, strict=False)
            except ValueError:
                raise ValueError(f"api.trusted_proxies: {item!r} is not an IP or network") from None
        return value


class AuthConfig(BaseModel):
    """``[auth]``: sign-in, sessions and the login limit (docs/security.md).

    Every field can be overridden by ``STONKS_AUTH_<FIELD>`` in the
    environment (e.g. ``STONKS_AUTH_COOKIE_SECURE=false``). Secrets never
    live here: ``STONKS_SECRET_KEYS`` and ``STONKS_API_TOKEN`` are env only."""

    model_config = ConfigDict(extra="forbid")

    #: Send cookies with ``Secure``. Browsers accept it on http://localhost too.
    cookie_secure: bool = True
    session_idle_hours: float = Field(default=12.0, gt=0)
    session_absolute_days: float = Field(default=7.0, gt=0)
    #: How long a password-only session may wait for its second factor.
    pending_login_minutes: float = Field(default=10.0, gt=0)
    #: Sensitive actions need a second factor verified this recently.
    step_up_minutes: float = Field(default=10.0, gt=0)
    #: Failed attempts allowed in the window: per account for passwords,
    #: per account for second-factor codes, and per client IP.
    max_failures: int = Field(default=5, ge=1)
    failure_window_minutes: float = Field(default=15.0, gt=0)
    totp_issuer: str = "Stonks"


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
    cost model for lab backtests. It defaults to
    ``CostModelSettings.realistic()`` (BL-13): a backtest without costs
    overstates every edge, so zero costs must be asked for explicitly (and
    are logged as a warning). Production can build the same model with
    ``settings.backtest.costs.build()``."""

    model_config = ConfigDict(extra="forbid")

    costs: CostModelSettings = Field(default_factory=CostModelSettings.realistic)
    #: ``[backtest.execution]``: the simulated broker's fill model
    #: (``[backtest.execution.fill]``, BL-30; absent = immediate fills at the
    #: next open) and cash settlement (``settlement_days``).
    execution: ExecutionSettings = ExecutionSettings()
    #: ``[backtest.construction]``: run lab and API backtests through the
    #: production construction pipeline (``None``: each strategy decides
    #: alone, today's behaviour).
    construction: ConstructionSettings | None = None


class LabSettings(BaseModel):
    """``[lab]``: defaults for ``stonks lab run``."""

    model_config = ConfigDict(extra="forbid")

    #: Benchmark every lab backtest is compared against (BL-22): ``auto``
    #: (SPY.US when the lake prices it, else the equal-weight universe),
    #: ``EW``, a ticker such as ``QQQ.US``, or ``none``. Requests override it.
    benchmark: str = Field(default="auto", max_length=32)
    #: Trading bars skipped between the train and the validation window
    #: (BL-20, P9); a strategy's ``label_horizon_bars`` raises it per run.
    embargo_bars: int = Field(default=0, ge=0)
    walk_forward: WalkForwardConfig = WalkForwardConfig()
    #: ``[lab.parallel]``: worker processes for tuning trials and sweeps
    #: (``max_workers = 0``: every core; 1: in-process) and BLAS threads each.
    parallel: ParallelSettings = ParallelSettings()
    #: Run the BL-37 data preflight before tuning: errors stop the run,
    #: warnings ride on the result (``--no-preflight``, request ``preflight``).
    preflight: bool = True
    #: Treat every preflight warning as an error (``--strict``, request
    #: ``strict_preflight``).
    strict_preflight: bool = False
    #: ``[lab.offload]``: run heavy lab jobs in a separate worker process
    #: (roadmap 14.9). Env: ``STONKS_LAB_EXECUTOR``.
    offload: LabOffloadSettings = LabOffloadSettings()


class AuditConfig(BaseModel):
    """``[audit]``: relative gaps above which the statement audit (BL-36,
    ``stonks audit statements``) flags a period."""

    model_config = ConfigDict(extra="forbid")

    balance: float = Field(default=AuditTolerances.balance, gt=0.0)
    net_income: float = Field(default=AuditTolerances.net_income, gt=0.0)
    cash: float = Field(default=AuditTolerances.cash, gt=0.0)
    gross_profit: float = Field(default=AuditTolerances.gross_profit, gt=0.0)
    quarterly_sum: float = Field(default=AuditTolerances.quarterly_sum, gt=0.0)

    def tolerances(self) -> AuditTolerances:
        return AuditTolerances(**self.model_dump())


class IngestConfig(BaseModel):
    """``[ingest]``: bar validation (``[ingest.quality]``) and the fallback
    source per primary (``[ingest.fallback]``, off by default)."""

    model_config = ConfigDict(extra="forbid")

    quality: DataQualityConfig = DataQualityConfig()
    fallback: FallbackConfig = FallbackConfig()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    lake: LakeConfig = LakeConfig()
    state: StateConfig = StateConfig()
    registry: RegistryConfig = RegistryConfig()
    logging: LoggingConfig = LoggingConfig()
    brokers: BrokersConfig = Field(default_factory=BrokersConfig)
    sources: SourcesConfig = SourcesConfig()
    production: ProductionConfig = Field(default_factory=ProductionConfig)
    notify: NotifyConfig = NotifyConfig()
    api: ApiConfig = Field(default_factory=ApiConfig)
    auth: AuthConfig = AuthConfig()
    backtest: BacktestSettings = BacktestSettings()
    lab: LabSettings = LabSettings()
    audit: AuditConfig = AuditConfig()
    golive: GoLivePolicy = GoLivePolicy()
    # ``[lifecycle]``: scheduled retraining and the model swap gate (roadmap 22.6).
    lifecycle: ModelLifecycleSettings = Field(default_factory=ModelLifecycleSettings)
    mcp: McpConfig = McpConfig()
    ingest: IngestConfig = IngestConfig()
    # ``[ensure]``: how on-demand bar fetches run (docs/universes.md).
    ensure: EnsureSettings = Field(default_factory=EnsureSettings)
    backup: BackupConfig = BackupConfig()
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    # ``[assistant]``: the in-app AI assistant (roadmap 20.4). The key is env only.
    assistant: AssistantConfig = Field(default_factory=AssistantConfig)
    # ``[telegram]``: the Telegram bot (roadmap 20.3). The token is env only.
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    # ``[factors]``: the factor panel cache (roadmap 22.2).
    factors: FactorSettings = FactorSettings()
    # ``[streaming]``: live price streams (roadmap 21.1). Off by default.
    streaming: StreamingSettings = Field(default_factory=StreamingSettings)
    # ``[screener]``: candidate cap, job threshold and result cache (roadmap 20.11).
    screener: ScreenerSettings = Field(default_factory=ScreenerSettings)
    # ``[engine]``: the intraday engine process (roadmap 21.2.5). Off by default.
    engine: EngineSettings = Field(default_factory=EngineSettings)


#: Secrets read straight from the environment by blocks that keep their own
#: settings (notify channels, broker connections, metrics, encryption keys).
ENV_ONLY_SECRETS: tuple[str, ...] = (
    "STONKS_SECRET_KEYS",
    "STONKS_METRICS_TOKEN",
    "STONKS_SMTP_PASSWORD",
    "STONKS_VAPID_PRIVATE_KEY",
    "STONKS_SNAPTRADE_CONSUMER_KEY",
    "STONKS_ASSISTANT_API_KEY",
    "STONKS_TELEGRAM_BOT_TOKEN",
    "STONKS_API_TOKEN",
    "EODHD_API_KEY",
    "ALPACA_API_KEY",
    "ALPACA_SECRET_KEY",
    "STONKS_IBKR_FLEX_TOKEN",
)


def secret_value(value: SecretStr | str | None) -> str | None:
    """The plain text of a secret setting (a raw str is accepted too)."""
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    return value


def _walk_secrets(obj: object, out: list[str]) -> None:
    if isinstance(obj, SecretStr):
        out.append(obj.get_secret_value())
    elif isinstance(obj, BaseModel):
        for name in type(obj).model_fields:
            _walk_secrets(getattr(obj, name, None), out)
    elif isinstance(obj, dict):
        for item in obj.values():
            _walk_secrets(item, out)
    elif isinstance(obj, list | tuple):
        for item in obj:
            _walk_secrets(item, out)


def configured_secrets(
    settings: Settings, *, environ: Mapping[str, str] | None = None
) -> list[str]:
    """Every credential value the server holds, for scrubbing text before it
    is logged, persisted or returned: each ``SecretStr`` in ``settings``, the
    plain secrets (vendor key, global webhook URL) and :data:`ENV_ONLY_SECRETS`.
    ``STONKS_SECRET_KEYS`` also adds each key of its ``id:key`` list."""
    env = os.environ if environ is None else environ
    values: list[str] = []
    _walk_secrets(settings, values)
    values.append(secret_value(settings.sources.eodhd.api_key) or "")
    values.append(settings.notify.webhook.url or "")
    for name in ENV_ONLY_SECRETS:
        raw = env.get(name)
        if not raw:
            continue
        values.append(raw)
        if name == "STONKS_SECRET_KEYS":
            for part in raw.split(","):
                values.append(part.split(":", 1)[-1].strip())
    out: list[str] = []
    for value in values:
        if value and len(value) >= 4 and value not in out:
            out.append(value)
    return out


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

    # The dev profile: reads from a loopback peer need no credential (the
    # Angular dev server proxies from 127.0.0.1). Never set it on a server.
    if os.environ.get("STONKS_PROFILE", "").strip().lower() == "dev":
        data.setdefault("api", {})["open_reads_on_loopback"] = True

    for name in AuthConfig.model_fields:
        value = os.environ.get(f"STONKS_AUTH_{name.upper()}")
        if value is not None and value.strip():
            data.setdefault("auth", {})[name] = value.strip()

    proxies = os.environ.get("STONKS_API_TRUSTED_PROXIES")
    if proxies is not None and proxies.strip():
        data.setdefault("api", {})["trusted_proxies"] = [
            p.strip() for p in proxies.split(",") if p.strip()
        ]

    api_host = os.environ.get("STONKS_API_HOST")
    if api_host is not None and api_host.strip():
        data.setdefault("api", {})["host"] = api_host.strip()

    hosts = os.environ.get("STONKS_API_ALLOWED_HOSTS")
    if hosts is not None and hosts.strip():
        data.setdefault("api", {})["allowed_hosts"] = [
            h.strip() for h in hosts.split(",") if h.strip()
        ]

    executor = os.environ.get("STONKS_LAB_EXECUTOR")
    if executor is not None and executor.strip():
        lab = data.setdefault("lab", {})
        lab.setdefault("offload", {})["executor"] = executor.strip()

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
