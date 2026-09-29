"""The no-broker signal path, end to end (docs/without-a-broker.md).

Stonks must always work with no broker at all: no IB Gateway, no broker
connection, the simulated broker as the default. These tests build the app
that way and walk the journey of a solo owner who only wants alerts:

1. daily prices and profiles from a canned source standing in for Yahoo;
2. a Strategy Studio rule draft and a catalog strategy, each tested;
3. both registered (On trial), the catalog one approved with the
   documented override and reason (the go-live check has no trial days);
4. both followed in Alerts only on the Simulated default portfolio;
5. Telegram linked with a one-time code through the bot, and signal alerts
   kept on Telegram only;
6. a trading run, then the delivery worker.

They check that no order, fill, broker connection or broker call exists,
that each new signal reaches Telegram once and only Telegram, that a rerun
sends nothing twice, that quiet hours hold signals back, that the scheduled
tick does the same on every scheduler backend, that the broker jobs skip
cleanly, that the CLI walks the same path, and that without a bot token the
rest still works and Telegram is simply not offered.
"""

from __future__ import annotations

import json
import math
import time
import zoneinfo
from collections.abc import Iterable, Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from stonks.accounts import Mode, Role, Scope, SubscriptionRepository
from stonks.accounts.models import DEFAULT_OWNER_ID, DEFAULT_PORTFOLIO_ID
from stonks.api import create_app
from stonks.app.context import AppContext
from stonks.app.services import Services, default_strategy_sources
from stonks.app.studio import RuleStrategySource
from stonks.auth import AuthService
from stonks.config import ApiConfig, LakeConfig, RegistryConfig, Settings, StateConfig
from stonks.connections.settings import ConnectionsConfig
from stonks.ingest.metadata_bundle import MetadataBundle
from stonks.ingest.schemas import FinancialStatementsBundle, RawPriceBar, TickerProfile
from stonks.ingest.sources.base import DataSource
from stonks.notify.channels import build_channels
from stonks.notify.prefs import Preference, PreferenceStore
from stonks.notify.settings import NotifySettings
from stonks.notify.worker import DeliveryWorker
from stonks.security import KeyRing, SecretBox, generate_key
from stonks.store.state import SqliteState
from stonks.telegram.bot import TelegramBot
from stonks.telegram.fake import FakeTelegramApi
from stonks.telegram.settings import TOKEN_ENV, TelegramConfig
from tests.integration.app.stepup import allow_step_up
from tests.integration.auth.helpers import make_service, session_principal

BASE = "https://testserver"
REMOTE = ("203.0.113.7", 50000)
TICKERS = ["RISE.US", "WAVE.US", "SLIDE.US"]
START, END = date(2024, 10, 1), date(2026, 4, 1)
AS_OF = END  # a Wednesday, NYSE open, with a bar
CHAT = 4242
MOMENTUM = "stonks.strategies.examples.momentum:Momentum"
TREND_RULE = {
    "version": 1,
    "name": "trend",
    "indicators": [
        {"id": "fast", "kind": "sma", "period": 5},
        {"id": "slow", "kind": "sma", "period": 20},
    ],
    "entry": {
        "type": "compare",
        "left": {"type": "indicator", "id": "fast"},
        "op": ">",
        "right": {"type": "indicator", "id": "slow"},
    },
    "rank": {"by": "fast"},
}
OVERRIDE_REASON = "Alerts only for me: no money follows it, so no trial days are needed."
#: The scheduler's jobs that talk to a broker, a gateway or a connection.
BROKER_JOBS = (
    "broker_health",
    "ibkr_reauth_reminder",
    "live_sod_check",
    "live_eod_check",
    "live_submit",
    "live_stops",
    "live_gate_days",
    "live_margin",
    "algo_slices",
    "ingest_borrow",
    "connections_sync",
    "price_check",
    "health",
)


# ---- the free data a solo owner has: Yahoo prices and profiles ----------------------


def _closes(ticker: str, n: int) -> list[float]:
    if ticker == "RISE.US":
        return [50.0 + 60.0 * i / (n - 1) + 1.5 * math.sin(i / 3) for i in range(n)]
    if ticker == "SLIDE.US":
        return [120.0 - 50.0 * i / (n - 1) + 1.0 * math.sin(i / 4) for i in range(n)]
    return [80.0 + 12.0 * math.sin(i / 15) for i in range(n)]


def _bars(ticker: str) -> list[RawPriceBar]:
    days = [d.date() for d in pd.bdate_range(START, END)]
    return [
        RawPriceBar(
            ticker=ticker,
            date=d,
            open=c,
            high=c * 1.01,
            low=c * 0.99,
            close=c,
            adj_close=c,
            volume=2_000_000,
        )
        for d, c in zip(days, _closes(ticker, len(days)), strict=True)
    ]


class YahooLikeSource(DataSource):
    """What the free Yahoo source gives: daily bars and each ticker's
    profile (its name and asset class). No fundamentals."""

    source_id = "yahoo"

    def __init__(self) -> None:
        self._prices = {t: _bars(t) for t in TICKERS}

    def list_tickers(self, exchange: str) -> list[str]:
        return list(TICKERS)

    def fetch_prices(
        self, ticker: str, since: date | None = None, until: date | None = None
    ) -> Iterable[RawPriceBar]:
        return [
            b
            for b in self._prices.get(ticker, [])
            if (since is None or b.date >= since) and (until is None or b.date <= until)
        ]

    def fetch_fundamentals(self, ticker: str) -> FinancialStatementsBundle:
        return FinancialStatementsBundle()

    def fetch_metadata(self, ticker: str) -> MetadataBundle:
        name = ticker.split(".")[0].title()
        return MetadataBundle(profile=TickerProfile(id=ticker, name=name, asset_class="equity"))


# ---- fixtures: an app with no broker of any kind -------------------------------------


class BrokerSpy:
    def __init__(self) -> None:
        self.calls: list[str] = []


@pytest.fixture
def spy(monkeypatch) -> BrokerSpy:
    """Records every broker built for a trading book and refuses to open a
    broker connection's trader."""
    import stonks.connections.service as connections
    import stonks.execution.brokers as brokers

    seen = BrokerSpy()
    real_make = brokers.make_broker

    def make_broker(settings: Any, *args: Any, **kwargs: Any):
        seen.calls.append(f"make_broker:{settings.brokers.kind}")
        return real_make(settings, *args, **kwargs)

    def open_trader(self: Any, *args: Any, **kwargs: Any):
        seen.calls.append("open_trader")
        raise AssertionError("no broker connection may be opened")

    monkeypatch.setattr(brokers, "make_broker", make_broker)
    monkeypatch.setattr(connections.ConnectionService, "open_trader", open_trader)
    return seen


@pytest.fixture
def telegram(monkeypatch) -> FakeTelegramApi:
    """The bot token is set, and every Telegram call (the bot and the
    notification channel) lands in one fake."""
    import stonks.telegram.bot as bot
    import stonks.telegram.channel as channel

    api = FakeTelegramApi()
    monkeypatch.setenv(TOKEN_ENV, "1:no-broker-test")
    monkeypatch.setattr(channel, "HttpTelegramApi", lambda *_a, **_k: api)
    monkeypatch.setattr(bot, "HttpTelegramApi", lambda *_a, **_k: api)
    return api


@pytest.fixture
def settings(tmp_path) -> Settings:
    out = Settings(
        lake=LakeConfig(path=tmp_path / "lake.duckdb"),
        state=StateConfig(path=tmp_path / "state.sqlite"),
        registry=RegistryConfig(artifacts_dir=tmp_path / "artifacts"),
        api=ApiConfig(ui_dist=tmp_path / "no-dist"),
    )
    out.api.allowed_hosts = ["testserver", "127.0.0.1"]
    out.production.universe = list(TICKERS)
    return out


@pytest.fixture
def auth(settings) -> AuthService:
    with SqliteState(settings.state.path) as state:
        state.migrate()
    box = SecretBox(KeyRing.parse(f"k1:{generate_key()}"))
    return make_service(settings.state.path, box=box, clock=lambda: datetime.now(UTC))


@pytest.fixture
def app(settings, auth):
    context = AppContext(settings, source_factory=YahooLikeSource)
    services = Services.create(
        context, strategy_sources=[*default_strategy_sources(), RuleStrategySource()]
    )
    application = create_app(settings, services=services, sse_poll_seconds=0.02)
    application.state.auth = auth
    # A browser session with a fresh second factor (a link code needs one).
    return allow_step_up(application)


@pytest.fixture
def owner(auth) -> dict[str, str]:
    """The solo owner: the install's admin, with a full token."""
    principal = session_principal(DEFAULT_OWNER_ID, Role.ADMIN)
    _, token = auth.create_token(principal, name="t", scopes=["read", "trade", "lab", "admin"])
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client(app) -> Iterator[TestClient]:
    with TestClient(app, client=REMOTE, base_url=BASE) as c:
        yield c


# ---- the journey's steps ------------------------------------------------------------


def _wait(client: TestClient, headers: dict, job_id: str, timeout: float = 300) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}", headers=headers).json()
        if job["status"] in ("succeeded", "failed", "cancelled"):
            assert job["status"] == "succeeded", job.get("error")
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish")


def _job(client: TestClient, headers: dict, path: str, body: dict) -> dict:
    resp = client.post(path, json=body, headers=headers)
    assert resp.status_code == 202, resp.text
    return _wait(client, headers, resp.json()["id"])


def _strategies(client: TestClient, owner: dict) -> tuple[str, str]:
    """Load the data, make both strategies, test them, put them on trial
    and approve the catalog one. Returns (catalog id, Studio rule id)."""
    since = START.isoformat()
    for kind in ("prices", "metadata"):
        body = {"kind": kind, "source": "yahoo", "tickers": TICKERS}
        _job(
            client, owner, "/api/ingest/runs", body | ({"since": since} if kind == "prices" else {})
        )

    # A Studio rule draft, backtested, then put on trial.
    draft = client.post(
        "/api/studio/drafts", json={"name": "Trend rule", "spec": TREND_RULE}, headers=owner
    )
    assert draft.status_code == 201, draft.text
    draft_id = draft.json()["id"]
    backtest = _job(
        client,
        owner,
        f"/api/studio/drafts/{draft_id}/backtests",
        {"universe": TICKERS, "start": "2025-06-02", "end": AS_OF.isoformat()},
    )
    assert backtest["result"]["equity"]
    registered = client.post(f"/api/studio/drafts/{draft_id}/register", headers=owner)
    assert registered.status_code == 200, registered.text
    assert registered.json()["strategy_status"] == "shadow"
    rule_id = registered.json()["registered_strategy_id"]

    # A catalog strategy, tested in the lab and put on trial.
    lab = _job(
        client,
        owner,
        "/api/lab/runs",
        {
            "strategy": {"class_path": MOMENTUM},
            "universe": TICKERS,
            "start": "2025-01-02",
            "end": AS_OF.isoformat(),
            "budget": 1,
            "survival_tests": ["oos"],
            "register_strategy": True,
        },
    )
    code_id = lab["result"]["registered_strategy_id"]
    assert client.get(f"/api/strategies/{code_id}", headers=owner).json()["status"] == "shadow"

    # Approve it: the go-live check has no trial days, so the owner uses
    # the documented override with a reason.
    refused = client.post(f"/api/strategies/{code_id}/promote", headers=owner)
    assert refused.status_code == 409
    approved = client.post(
        f"/api/strategies/{code_id}/promote",
        json={"override": True, "reason": OVERRIDE_REASON},
        headers=owner,
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "active"
    return code_id, rule_id


def _follow_alerts_only(client: TestClient, owner: dict, strategy_ids: list[str]) -> None:
    """Alerts only on the Simulated default portfolio. Approving made that
    portfolio follow the approved strategy in Paper: a second follow there
    is refused and names it, so its mode switches instead."""
    [default] = [
        p
        for p in client.get("/api/portfolios", headers=owner).json()["items"]
        if p["id"] == DEFAULT_PORTFOLIO_ID
    ]
    assert default["kind"] == "simulated"
    for sid in strategy_ids:
        body = {"strategy_id": sid, "portfolio_id": DEFAULT_PORTFOLIO_ID, "mode": "notify"}
        resp = client.post("/api/subscriptions", json=body, headers=owner)
        if resp.status_code == 409:
            [existing] = [
                s
                for s in client.get("/api/subscriptions", headers=owner).json()["items"]
                if s["strategy_id"] == sid and s["portfolio_id"] == DEFAULT_PORTFOLIO_ID
            ]
            assert existing["id"] in resp.json()["detail"]
            resp = client.patch(
                f"/api/subscriptions/{existing['id']}",
                json={"mode": "notify", "reason": "alerts only"},
                headers=owner,
            )
            assert resp.status_code == 200, resp.text
        else:
            assert resp.status_code == 201, resp.text
        assert resp.json()["mode"] == "notify"


def _link_telegram(client: TestClient, owner: dict, app, api: FakeTelegramApi) -> None:
    status = client.get("/api/telegram/link", headers=owner).json()
    assert status["bot_configured"] is True and status["linked"] is False
    code = client.post("/api/telegram/link-code", headers=owner)
    assert code.status_code == 201, code.text
    bot = TelegramBot(api, app.state.services.context, TelegramConfig(enabled=True))
    api.push(CHAT, f"/link {code.json()['code']}", username="owner_tg")
    bot.poll_once(timeout=0)
    assert api.replies(CHAT)[-1].startswith("Linked.")
    assert client.get("/api/telegram/link", headers=owner).json()["linked"] is True
    api.sent.clear()


def _signals_on_telegram_only(client: TestClient, owner: dict) -> dict:
    prefs = client.get("/api/notifications/preferences", headers=owner).json()
    assert "telegram" in prefs["channels"]
    only = [
        {"category": "signal", "channel": ch, "enabled": ch == "telegram"}
        for ch in prefs["channels"]
    ]
    resp = client.put("/api/notifications/preferences", json={"preferences": only}, headers=owner)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _count(settings: Settings, table: str, where: str = "") -> int:
    with SqliteState(settings.state.path) as state:
        return int(state.sql(f"SELECT COUNT(*) FROM {table} {where}")[0][0])


def _nothing_traded(settings: Settings, spy: BrokerSpy) -> None:
    assert _count(settings, "orders") == 0
    assert _count(settings, "fills") == 0
    assert _count(settings, "broker_connections") == 0
    assert _count(settings, "risk_halts") == 0
    assert spy.calls == []


def _expected(settings: Settings, strategy_ids: list[str]) -> list[tuple[str, str]]:
    """What the notify hook announces, as (title, strategy) pairs: each
    followed strategy's signal events of the day, risk events left out, at
    most three each."""
    from stonks.production.hooks.notifications import MAX_PICKS
    from stonks.production.signals import events_for

    with SqliteState(settings.state.path) as state:
        events = events_for(state, AS_OF, sorted(strategy_ids))
    out: list[tuple[str, str]] = []
    per: dict[str, int] = {}
    for e in events:
        if e.kind == "risk" or per.get(e.strategy_id, 0) >= MAX_PICKS:
            continue
        per[e.strategy_id] = per.get(e.strategy_id, 0) + 1
        out.append((f"{e.ticker}: {e.kind} signal", e.strategy_id))
    return sorted(out)


def _sent(api: FakeTelegramApi) -> list[tuple[str, str]]:
    """(title, strategy) of each message sent to the owner's chat: the
    first line is the title, the second starts with the strategy id."""
    out = []
    for chat, text in api.sent:
        assert chat == str(CHAT), chat
        title, body = text.splitlines()[:2]
        out.append((title, body.split(" on ", 1)[0]))
    return sorted(out)


def _channels(settings: Settings) -> set[str]:
    with SqliteState(settings.state.path) as state:
        rows = state.sql(
            "SELECT d.channel FROM notification_deliveries d JOIN notification_outbox o"
            " ON o.id = d.notification_id WHERE o.category = 'signal'"
        )
    return {r["channel"] for r in rows}


def _deliver(settings: Settings, at: datetime) -> int:
    """One pass of the delivery worker, as the scheduler runs it."""
    notify = NotifySettings.from_env()
    with SqliteState(settings.state.path) as state:
        worker = DeliveryWorker(state, build_channels(notify), notify.outbox, clock=lambda: at)
        return worker.run_once().sent


# ---- the journey through the API --------------------------------------------------------


def test_no_broker_is_configured(settings):
    assert settings.brokers.kind == "simulated"
    assert settings.brokers.ibkr.gateways == {}
    assert ConnectionsConfig().enabled_providers == ()


def test_a_solo_owner_gets_signals_on_telegram_with_no_broker(
    client, owner, settings, spy, telegram, app
):
    code_id, rule_id = _strategies(client, owner)
    _follow_alerts_only(client, owner, [code_id, rule_id])
    _link_telegram(client, owner, app, telegram)
    _signals_on_telegram_only(client, owner)

    tick = _job(client, owner, "/api/ticks", {"as_of": AS_OF.isoformat()})
    # No book trades, so the run has nothing to place: "noop", never an error.
    assert tick["result"]["status"] in ("ok", "noop"), tick["result"]
    _nothing_traded(settings, spy)

    # Each new signal of both strategies (the On trial one too) reaches
    # Telegram once, and only Telegram.
    expected = _expected(settings, [code_id, rule_id])
    assert {sid for _, sid in expected} == {code_id, rule_id}, expected
    assert _channels(settings) == {"telegram"}
    assert _deliver(settings, datetime.now(UTC) + timedelta(seconds=1)) == len(expected)
    assert _sent(telegram) == expected
    for _, text in telegram.sent:  # each links to its strategy's page
        _, body, link = text.splitlines()[:3]
        assert link.endswith(f"/strategies/{body.split(' on ', 1)[0]}"), text

    # The feed shows them too.
    feed = client.get("/api/notifications", headers=owner).json()
    titles = sorted(n["title"] for n in feed["items"] if n["category"] == "signal")
    assert titles == sorted(title for title, _ in expected)

    # A rerun of the same day sends nothing twice.
    _job(client, owner, "/api/ticks", {"as_of": AS_OF.isoformat()})
    assert _deliver(settings, datetime.now(UTC) + timedelta(seconds=2)) == 0
    assert _sent(telegram) == expected
    _nothing_traded(settings, spy)


def test_quiet_hours_hold_signals_until_they_end(client, owner, settings, spy, telegram, app):
    code_id, rule_id = _strategies(client, owner)
    _follow_alerts_only(client, owner, [code_id, rule_id])
    _link_telegram(client, owner, app, telegram)
    prefs = _signals_on_telegram_only(client, owner)
    quiet = client.put(
        "/api/notifications/quiet-hours", json={"start": "02:00", "end": "04:00"}, headers=owner
    )
    assert quiet.status_code == 200, quiet.text

    _job(client, owner, "/api/ticks", {"as_of": AS_OF.isoformat()})
    expected = _expected(settings, [code_id, rule_id])
    assert expected

    zone = zoneinfo.ZoneInfo(prefs["timezone"])
    tomorrow = (datetime.now(zone) + timedelta(days=1)).date()
    inside = datetime(tomorrow.year, tomorrow.month, tomorrow.day, 3, tzinfo=zone)
    assert _deliver(settings, inside.astimezone(UTC)) == 0
    assert telegram.sent == []
    after = inside + timedelta(hours=1, minutes=30)
    assert _deliver(settings, after.astimezone(UTC)) == len(expected)
    # Held back together, they go out as one summary message.
    [(chat, text)] = telegram.sent
    assert chat == str(CHAT) and str(len(expected)) in text
    _nothing_traded(settings, spy)


def test_without_a_bot_token_the_rest_works_and_telegram_is_not_offered(
    client, owner, settings, spy, app, monkeypatch
):
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    code_id, rule_id = _strategies(client, owner)
    _follow_alerts_only(client, owner, [code_id, rule_id])

    status = client.get("/api/telegram/link", headers=owner).json()
    assert status["bot_configured"] is False
    code = client.post("/api/telegram/link-code", headers=owner)
    assert code.status_code == 503 and code.json()["code"] == "not_configured"
    prefs = client.get("/api/notifications/preferences", headers=owner).json()
    assert "telegram" not in prefs["channels"]

    tick = _job(client, owner, "/api/ticks", {"as_of": AS_OF.isoformat()})
    assert tick["result"]["status"] in ("ok", "noop"), tick["result"]
    _nothing_traded(settings, spy)
    expected = _expected(settings, [code_id, rule_id])
    feed = client.get("/api/notifications", headers=owner).json()
    titles = sorted(n["title"] for n in feed["items"] if n["category"] == "signal")
    assert titles == sorted(title for title, _ in expected) and titles
    assert "telegram" not in _channels(settings)


# ---- the scheduled tick on every backend ------------------------------------------------


def _run_context(settings: Settings, executor: Any, action: str, **params: Any):
    from stonks.notify import Notification, Notifier
    from stonks.scheduling.jobs import JobSpec, RunContext
    from stonks.scheduling.triggers import Fire, SessionTrigger

    class Quiet(Notifier):
        def __init__(self) -> None:
            self.sent: list[Notification] = []

        def _send(self, n: Notification) -> None:
            self.sent.append(n)

    at = datetime(AS_OF.year, AS_OF.month, AS_OF.day, 21, 30, tzinfo=UTC)
    notifier = Quiet()
    ctx = RunContext(
        spec=JobSpec(action, action, SessionTrigger("XNYS"), params=params),
        fire=Fire(at, AS_OF, AS_OF.isoformat()),
        run_id=f"srun_{action}",
        now=at,
        settings=settings,
        notifier=notifier,
        executor=executor,
    )
    return ctx, notifier


def _bridge(tc: TestClient):
    import httpx2

    hop = {"content-length", "content-encoding", "transfer-encoding"}

    def handler(request: httpx2.Request) -> httpx2.Response:
        headers = {k: v for k, v in request.headers.items() if k.lower() not in hop}
        resp = tc.request(
            request.method, request.url.raw_path.decode(), headers=headers, content=request.content
        )
        out = [(k, v) for k, v in resp.headers.items() if k.lower() not in hop | {"host"}]
        return httpx2.Response(resp.status_code, headers=out, content=resp.content)

    return httpx2.MockTransport(handler)


@pytest.mark.parametrize("backend", ["local", "in_process", "api"])
def test_the_scheduled_tick_sends_the_signals_on_every_backend(
    backend, app, owner, settings, spy, telegram, auth
):
    from stonks.scheduling.api_backend import ApiExecutor
    from stonks.scheduling.api_client import SchedulerApiClient
    from stonks.scheduling.in_process import InProcessExecutor
    from stonks.scheduling.local import LocalExecutor

    with TestClient(app, client=REMOTE, base_url=BASE) as client:
        code_id, rule_id = _strategies(client, owner)
        _follow_alerts_only(client, owner, [code_id, rule_id])
        _link_telegram(client, owner, app, telegram)
        _signals_on_telegram_only(client, owner)
        if backend == "in_process":
            executor: Any = InProcessExecutor(app.state.services)
            outcome = executor.execute(_run_context(settings, executor, "tick")[0])
        elif backend == "api":
            _, token = auth.create_token(
                session_principal(DEFAULT_OWNER_ID, Role.ADMIN),
                name="scheduler",
                scopes=["read", "trade", "lab", "admin"],
            )
            with TestClient(app, client=("127.0.0.1", 50000)) as local:
                api = SchedulerApiClient(
                    "http://127.0.0.1:8000", token=token, transport=_bridge(local)
                )
                executor = ApiExecutor(api, poll_seconds=0.02)
                try:
                    outcome = executor.execute(_run_context(settings, executor, "tick")[0])
                finally:
                    executor.close()
    if backend == "local":
        # The local backend opens the stores itself, once the API let go.
        import stonks.scheduling.local as local_backend

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(local_backend, "build_source", lambda *_a, **_k: YahooLikeSource())
            executor = LocalExecutor()
            outcome = executor.execute(_run_context(settings, executor, "tick")[0])

    assert outcome.status == "succeeded", outcome.detail
    _nothing_traded(settings, spy)
    expected = _expected(settings, [code_id, rule_id])
    assert {sid for _, sid in expected} == {code_id, rule_id}
    assert _channels(settings) == {"telegram"}
    assert _deliver(settings, datetime.now(UTC) + timedelta(seconds=1)) == len(expected)
    assert _sent(telegram) == expected


def test_the_broker_jobs_skip_cleanly_and_open_no_halt(settings, spy):
    """Every scheduler job that talks to a broker, a gateway or a broker
    connection, plus health, with no broker at all: each skips or succeeds
    and nothing halts trading."""
    from stonks.scheduling.config import SchedulerConfig, default_jobs
    from stonks.scheduling.local import LocalExecutor
    from stonks.store.lake import DuckDBLake

    with DuckDBLake(settings.lake.path) as lake:
        lake.migrate()
    with SqliteState(settings.state.path) as state:
        state.migrate()
    settings.production.universe = []  # freshness is not what this checks
    jobs = {j.name: j for j in default_jobs()}
    assert set(BROKER_JOBS) <= set(jobs), set(BROKER_JOBS) - set(jobs)
    config = SchedulerConfig()
    assert config.deliver_notifications is True
    executor = LocalExecutor()
    for name in BROKER_JOBS:
        job = jobs[name]
        ctx, notifier = _run_context(settings, executor, job.action, **job.params)
        outcome = executor.execute(ctx)
        assert outcome.status in ("skipped", "succeeded"), (name, outcome.detail)
        assert notifier.sent == [], (name, notifier.sent)
    _nothing_traded(settings, spy)


# ---- the same journey through the CLI -------------------------------------------------


@pytest.fixture
def cli_home(tmp_path, monkeypatch):
    """A fresh install folder with the shipped settings that matter here
    and no broker, and the Yahoo stand-in behind every source id."""
    import stonks.ingest.sources.registry as registry

    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    universe = ", ".join(f'"{t}"' for t in TICKERS)
    (tmp_path / "config" / "default.toml").write_text(
        '[lake]\npath = "data/lake.duckdb"\n\n[state]\npath = "data/state.sqlite"\n\n'
        '[registry]\nartifacts_dir = "data/artifacts"\n\n'
        f"[production]\nuniverse = [{universe}]\ninitial_cash = 10000.0\n"
        'paper_fills = "next_open"\n\n[brokers]\nkind = "simulated"\n',
        encoding="utf-8",
    )
    for source_id in list(registry._FACTORIES):
        monkeypatch.setitem(registry._FACTORIES, source_id, lambda _cfg: YahooLikeSource())
    return tmp_path


def _cli():
    from stonks.cli import app as cli_app

    return cli_app


def _invoke(*args: str) -> str:
    result = CliRunner().invoke(_cli(), list(args))
    assert result.exit_code == 0, f"stonks {' '.join(args)}\n{result.output}"
    return result.output


def test_the_cli_walks_the_same_path(cli_home, spy, telegram):
    state_path = cli_home / "data" / "state.sqlite"
    tickers = ",".join(TICKERS)
    _invoke("db", "init")
    _invoke("ingest", "prices", "--source", "yahoo", "--tickers", tickers, "--since", "2024-10-01")
    _invoke("ingest", "metadata", "--source", "yahoo", "--tickers", tickers)

    lab_json = cli_home / "lab.json"
    _invoke(
        "lab",
        "run",
        "buy_and_hold",
        "--params",
        '{"ticker": "RISE.US", "allocation": 1.0}',
        "--tickers",
        "RISE.US",
        "--start",
        "2025-01-02",
        "--end",
        AS_OF.isoformat(),
        "--tests",
        "oos",
        # Buy and hold makes one trade: judge it on the flat Sharpe gate.
        "--test-option",
        "oos.mode=sharpe",
        "--test-option",
        "oos.min_trades=0",
        "--register",
        "--workers",
        "1",
        "--hypothesis",
        "Holding a steadily rising stock earns its drift; the canned RISE.US path rises.",
        "--json-out",
        str(lab_json),
    )
    lab = json.loads(lab_json.read_text(encoding="utf-8"))
    sid = lab["registered_id"]
    assert sid, "the lab run should put the strategy on trial"
    check = CliRunner().invoke(_cli(), ["golive", "check", sid])
    assert check.exit_code == 1, check.output  # no trial days yet
    _invoke("registry", "promote", sid, "--override", "--reason", OVERRIDE_REASON)

    # Following has no CLI command (the console, the API and MCP do it).
    with SqliteState(state_path) as state:
        owner = Scope(user_id=DEFAULT_OWNER_ID, role=Role.ADMIN)
        subs = SubscriptionRepository(state)
        [paper] = [s for s in subs.list_for_user(owner) if s.strategy_id == sid]
        assert (paper.portfolio_id, paper.mode) == (DEFAULT_PORTFOLIO_ID, Mode.PAPER)
        subs.set_mode(owner, paper.id, Mode.NOTIFY, reason="alerts only")
        PreferenceStore(state).set(
            DEFAULT_OWNER_ID,
            [Preference("signal", ch, False) for ch in ("webpush", "email", "webhook")],
            now=datetime.now(UTC),
        )

    code = _invoke("telegram", "link-code", "--user", DEFAULT_OWNER_ID)
    telegram.push(CHAT, f"/link {code.split('code ')[1].split(',')[0].strip()}")
    assert "handled 1 update" in _invoke("telegram", "poll", "--once")
    assert "linked to" in _invoke("telegram", "status", "--user", DEFAULT_OWNER_ID)
    telegram.sent.clear()

    out = _invoke("tick", "--as-of", AS_OF.isoformat())
    assert "orders=0" in out and "fills=0" in out, out
    from stonks.notify.__main__ import main as notify_main

    assert notify_main(["deliver", "--state", str(state_path)]) == 0
    with SqliteState(state_path) as state:
        orders = state.sql("SELECT COUNT(*) FROM orders")[0][0]
        fills = state.sql("SELECT COUNT(*) FROM fills")[0][0]
    assert (orders, fills) == (0, 0)
    assert spy.calls == []
    sent = _sent(telegram)
    assert sent and {s for _, s in sent} == {sid}


# ---- a keyless install keeps its prices fresh ---------------------------------------


@pytest.mark.parametrize("backend", ["local", "in_process", "api"])
def test_a_keyless_install_keeps_its_prices_fresh_on_every_backend(
    backend, settings, auth, owner, monkeypatch
):
    """With no EODHD key the scheduled price update reads Yahoo, which needs
    none, instead of failing every day and leaving the signals stale."""
    import stonks.ingest.sources.registry as registry
    from stonks.scheduling.api_backend import ApiExecutor
    from stonks.scheduling.api_client import SchedulerApiClient
    from stonks.scheduling.in_process import InProcessExecutor
    from stonks.scheduling.local import LocalExecutor
    from stonks.store.lake import DuckDBLake

    monkeypatch.delenv("EODHD_API_KEY", raising=False)
    monkeypatch.setitem(registry._FACTORIES, "yahoo", lambda _cfg: YahooLikeSource())
    assert settings.sources.eodhd.api_key is None
    app = create_app(settings, services=Services.create(AppContext(settings)))
    app.state.auth = auth
    params = {"lookback_days": 30}
    if backend == "local":
        with DuckDBLake(settings.lake.path) as lake:
            lake.migrate()
        executor: Any = LocalExecutor()
        outcome = executor.execute(_run_context(settings, executor, "ingest_prices", **params)[0])
    elif backend == "in_process":
        with TestClient(app, client=REMOTE, base_url=BASE):
            executor = InProcessExecutor(app.state.services)
            ctx = _run_context(settings, executor, "ingest_prices", **params)[0]
            outcome = executor.execute(ctx)
    else:
        with TestClient(app, client=("127.0.0.1", 50000)) as local:
            token = owner["Authorization"].removeprefix("Bearer ")
            api = SchedulerApiClient("http://127.0.0.1:8000", token=token, transport=_bridge(local))
            executor = ApiExecutor(api, poll_seconds=0.02)
            try:
                ctx = _run_context(settings, executor, "ingest_prices", **params)[0]
                outcome = executor.execute(ctx)
            finally:
                executor.close()
    assert outcome.status == "succeeded", outcome.detail
    with DuckDBLake(settings.lake.path) as lake:
        latest = lake.sql("SELECT MAX(date) FROM prices WHERE ticker = 'RISE.US'")
    assert str(latest.iloc[0, 0])[:10] == AS_OF.isoformat()
