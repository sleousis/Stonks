"""A seeded Stonks stack for end-to-end tests: stores, data, people, server.

``build_stack(root)`` lays out a throwaway install under ``root``:

- ``config/default.toml``: paths inside ``root``, the built console, one
  worker for lab runs, the simulated broker;
- ``data/``: both stores migrated, canned bars ingested through the ingest
  pipeline (a split, a dividend, a benchmark and a crypto ticker), four
  active buy-and-hold strategies (one per tradable equity, so a tick limited
  to one ticker always has one order to place) and one shadow strategy per viewport;
- people: the bootstrap admin and a trader, both enrolled with known TOTP
  secrets, and a trader portfolio with its own snapshot.

``start_server(stack)`` runs ``stonks serve`` (through :mod:`tests.e2e.serve`,
which swaps every data source for the canned one) on a free localhost port
and waits for ``/api/health``.

Run it by hand to click around the seeded console::

    uv run python -m tests.e2e.stack --root .e2e-stack
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pyotp

from stonks.accounts.default_book import ensure_default_subscription
from stonks.accounts.models import DEFAULT_OWNER_ID, DEFAULT_PORTFOLIO_ID, Mode
from stonks.accounts.portfolios import PortfolioRepository
from stonks.accounts.scope import Scope
from stonks.accounts.users import UserRepository
from stonks.auth.passwords import PasswordHasher
from stonks.core.protocols import SurvivalReport
from stonks.ingest.pipeline import IngestPipeline
from stonks.registry.store import StrategyRegistry
from stonks.security.crypto import SecretBox, generate_key
from stonks.store.lake import DuckDBLake
from stonks.store.state import SqliteState
from stonks.strategies.examples.buy_and_hold import BuyAndHold
from tests.e2e.fake_market import (
    BENCHMARK,
    CRYPTO,
    EQUITIES,
    TICKERS,
    CannedDataSource,
    build_market,
    last_business_day,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DIST = REPO_ROOT / "web" / "dist"

ADMIN_EMAIL = "admin@e2e.test"
TRADER_EMAIL = "trader@e2e.test"
PASSWORD = "correct horse battery staple"
# Fixed so a failing run can be replayed in an authenticator app.
ADMIN_TOTP = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
TRADER_TOTP = "KRSXG5CTMVRXEZLUKRSXG5CTMVRXEZLU"

# One shadow strategy per viewport, so each promote journey starts in shadow.
SHADOW_IDS = {"desktop": "bah_shadow_desktop", "phone": "bah_shadow_phone"}
ACTIVE_IDS = {t: f"bah_{t.split('.')[0].lower()}" for t in (*EQUITIES, CRYPTO)}
TRADER_PORTFOLIO_NAME = "Trader paper book"
TRADER_CASH = 4321.0
ADMIN_CASH = 10000.0


# Cheap argon2 parameters: the server reads them from each stored hash.
_HASHER = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1)


@dataclass
class Person:
    email: str
    password: str
    totp_secret: str
    user_id: str
    role: str
    #: The last TOTP step the server accepted for this person (never reused).
    last_step: int = -1

    def code(self) -> str:
        """A fresh code for a step the server has not seen, waiting for the
        next 30 s step when both usable ones are spent."""
        totp = pyotp.TOTP(self.totp_secret)
        while True:
            now = datetime.now(UTC)
            current = totp.timecode(now)
            step = max(current, self.last_step + 1)
            if step <= current + 1:
                self.last_step = step
                return totp.generate_otp(step)
            time.sleep(1.0)


@dataclass
class Stack:
    root: Path
    data_dir: Path
    config_path: Path
    env: dict[str, str]
    admin: Person
    trader: Person
    market_end: date
    trader_portfolio: str
    base_url: str = ""
    process: subprocess.Popen | None = None
    log_path: Path | None = None
    _tick_days: list[date] = field(default_factory=list)

    def add_person(self, role: str, email: str, display_name: str) -> Person:
        """A person with a password and no second factor yet (first sign-in)."""
        with SqliteState(self.data_dir / "state.sqlite") as state:
            user = UserRepository(state).create(
                display_name=display_name, role=role, actor="test:e2e", email=email
            )
            state.execute(
                "UPDATE users SET password_hash = ?, password_changed_at = ? WHERE id = ?",
                [_HASHER.hash(PASSWORD), datetime.now(UTC).isoformat(), user.id],
            )
        return Person(email, PASSWORD, "", user.id, role)

    def notify(self, person: Person, title: str, body: str, category: str = "signal") -> str:
        """Publish one notification to ``person``'s feed, as the tick would."""
        from stonks.notify.events import Audience, Event
        from stonks.notify.router import NotificationRouter

        with SqliteState(self.data_dir / "state.sqlite") as state:
            result = NotificationRouter(state, {}).publish(
                Event(
                    category=category,  # type: ignore[arg-type]
                    title=title,
                    body=body,
                    audience=Audience.users(person.user_id),
                    deep_link="/strategies",
                )
            )
        return result.notification_ids[0]

    def next_tick_date(self) -> date:
        """Increasing business days: a real tick may not go behind the last
        one, so each tick in the suite takes the next date."""
        return self._tick_days.pop(0)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _config_text(data_dir: Path, dist: Path) -> str:
    def p(path: Path) -> str:
        return path.as_posix()

    universe = ", ".join(f'"{t}"' for t in TICKERS)
    return f"""
[lake]
path = "{p(data_dir / "lake.duckdb")}"

[state]
path = "{p(data_dir / "state.sqlite")}"

[registry]
artifacts_dir = "{p(data_dir / "artifacts")}"

[logging]
level = "WARNING"

[production]
universe = [{universe}]
initial_cash = 10000.0

[lab]
benchmark = "{BENCHMARK}"

[lab.parallel]
max_workers = 1

[brokers]
kind = "simulated"

[backup]
dir = "{p(data_dir.parent / "backups")}"

[notify]
backends = ["log", "store"]

[api]
ui_dist = "{p(dist)}"
max_concurrent_jobs = 2

[scheduler]
catch_up = "none"
""".lstrip()


def _seed_market(data_dir: Path, market_end: date) -> None:
    market = build_market(market_end)
    source = CannedDataSource(market)
    lake = DuckDBLake(data_dir / "lake.duckdb")
    try:
        lake.migrate()
        pipeline = IngestPipeline(source, lake)
        pipeline.run_metadata(list(TICKERS))
        pipeline.run_prices(list(TICKERS), until=market_end)
    finally:
        lake.close()


def _seal_totp(box: SecretBox, user_id: str, secret: str) -> str:
    # Same associated data as AuthService, so the server can open it.
    return box.seal(secret.encode(), aad=f"users.totp_secret:{user_id}").token


def _seed_people(state: SqliteState, box: SecretBox) -> tuple[Person, Person, str]:
    hasher = _HASHER
    users = UserRepository(state)
    trader_user = users.create(
        display_name="Tess Trader", role="trader", actor="test:e2e", email=TRADER_EMAIL
    )
    now = datetime.now(UTC).isoformat()
    for user_id, email, secret in (
        (DEFAULT_OWNER_ID, ADMIN_EMAIL, ADMIN_TOTP),
        (trader_user.id, TRADER_EMAIL, TRADER_TOTP),
    ):
        state.execute(
            "UPDATE users SET email = ?, password_hash = ?, password_changed_at = ?,"
            " totp_secret_enc = ?, mfa_enrolled_at = ? WHERE id = ?",
            [email, hasher.hash(PASSWORD), now, _seal_totp(box, user_id, secret), now, user_id],
        )
    state.execute("UPDATE users SET display_name = 'Ada Admin' WHERE id = ?", [DEFAULT_OWNER_ID])

    portfolio = PortfolioRepository(state).create(
        Scope.for_user(trader_user), name=TRADER_PORTFOLIO_NAME, initial_cash=TRADER_CASH
    )
    admin = Person(ADMIN_EMAIL, PASSWORD, ADMIN_TOTP, DEFAULT_OWNER_ID, "admin")
    trader = Person(TRADER_EMAIL, PASSWORD, TRADER_TOTP, trader_user.id, "trader")
    return admin, trader, portfolio.id


def _seed_snapshot(state: SqliteState, portfolio_id: str, as_of: date, cash: float) -> None:
    """A starting snapshot (cash only), so each book has a value before any tick."""
    tick_id = f"tick-e2e-seed-{portfolio_id}"
    state.execute(
        "INSERT INTO tick_runs (id, started_at, finished_at, status) VALUES (?, ?, ?, 'ok')",
        [tick_id, f"{as_of}T21:00:00+00:00", f"{as_of}T21:01:00+00:00"],
    )
    state.execute(
        "INSERT INTO portfolio_snapshots (tick_id, portfolio_id, as_of, taken_at, cash,"
        " positions_json, total_value) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [tick_id, portfolio_id, as_of.isoformat(), f"{as_of}T21:00:00+00:00", cash, "{}", cash],
    )


def _seed_strategies(state: SqliteState, artifacts: Path) -> None:
    registry = StrategyRegistry(state=state, artifacts_dir=artifacts)
    report = SurvivalReport(test_id="oos", passed=True, metrics={"sharpe_oos": 1.2})
    for ticker, sid in ACTIVE_IDS.items():
        registry.register(BuyAndHold({"ticker": ticker, "allocation": 0.2}), [report], sid)
        registry.set_status(
            sid, "active", actor="test:e2e", reason="seeded for the e2e stack", override=True
        )
        # like a promotion: the default book follows every active strategy
        ensure_default_subscription(state, sid, Mode.PAPER)
    # Registered strategies start in shadow: the go-live gate refuses it.
    for sid in SHADOW_IDS.values():
        registry.register(BuyAndHold({"ticker": "CCC.US", "allocation": 0.5}), [report], sid)


def build_stack(root: Path, *, dist: Path = DEFAULT_DIST, market_end: date | None = None) -> Stack:
    root = Path(root).resolve()
    data_dir = root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (root / "config").mkdir(exist_ok=True)
    config_path = root / "config" / "default.toml"
    config_path.write_text(_config_text(data_dir, Path(dist).resolve()), encoding="utf-8")

    market_end = market_end or last_business_day(datetime.now(UTC).date())
    env = {
        "STONKS_SECRET_KEYS": f"e2e:{generate_key()}",
        "STONKS_AUTH_COOKIE_SECURE": "false",
        "STONKS_CONNECTIONS_ENABLED_PROVIDERS": "fake",
        "STONKS_E2E_MARKET_END": market_end.isoformat(),
        "STONKS_LOG_LEVEL": "WARNING",
        # A second factor counts as fresh for 15 s only, so the kill switch
        # journey sees the step-up prompt without waiting ten minutes.
        "STONKS_AUTH_STEP_UP_MINUTES": "0.25",
    }
    box = SecretBox.from_env(env)

    _seed_market(data_dir, market_end)
    with SqliteState(data_dir / "state.sqlite") as state:
        state.migrate()
        admin, trader, trader_pf = _seed_people(state, box)
        _seed_snapshot(state, trader_pf, market_end - timedelta(days=25), TRADER_CASH)
        _seed_snapshot(state, DEFAULT_PORTFOLIO_ID, market_end - timedelta(days=25), ADMIN_CASH)
        _seed_strategies(state, data_dir / "artifacts")

    days = [market_end - timedelta(days=i) for i in range(20)]
    tick_days = sorted(d for d in days if d.weekday() < 5)[-8:]
    return Stack(
        root=root,
        data_dir=data_dir,
        config_path=config_path,
        env=env,
        admin=admin,
        trader=trader,
        market_end=market_end,
        trader_portfolio=trader_pf,
        _tick_days=tick_days,
    )


def _server_env(stack: Stack) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("STONKS_", "ALPACA_")) and k != "EODHD_API_KEY"
    }
    env.update(stack.env)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO_ROOT), env.get("PYTHONPATH")]))
    env["PYTHONUNBUFFERED"] = "1"
    return env


def start_server(stack: Stack, *, port: int | None = None, timeout: float = 60.0) -> Stack:
    port = port or free_port()
    stack.log_path = stack.root / "server.log"
    log = open(stack.log_path, "wb")  # noqa: SIM115 - closed with the process
    stack.process = subprocess.Popen(
        [sys.executable, "-m", "tests.e2e.serve", "--port", str(port)],
        cwd=stack.root,
        env=_server_env(stack),
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    stack.base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if stack.process.poll() is not None:
            raise RuntimeError(
                f"server exited early:\n{stack.log_path.read_text(errors='replace')}"
            )
        try:
            with urllib.request.urlopen(f"{stack.base_url}/api/health", timeout=2) as resp:
                if resp.status == 200:
                    return stack
        except OSError:
            time.sleep(0.25)
    stop_server(stack)
    raise RuntimeError("server did not answer /api/health in time")


def stop_server(stack: Stack) -> None:
    proc = stack.process
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path(".e2e-stack"))
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    if args.root.exists() and any(args.root.iterdir()):
        parser.error(f"{args.root} is not empty; pick a new --root")
    stack = start_server(build_stack(args.root), port=args.port)
    print(f"Stonks e2e stack on {stack.base_url}")  # noqa: T201 - output of the manual stack command
    for person in (stack.admin, stack.trader):
        print(f"  {person.role}: {person.email} / {person.password}  TOTP {person.totp_secret}")  # noqa: T201 - output of the manual stack command
    print("Ctrl+C to stop.")  # noqa: T201 - output of the manual stack command
    try:
        assert stack.process is not None
        stack.process.wait()
    except KeyboardInterrupt:
        stop_server(stack)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
