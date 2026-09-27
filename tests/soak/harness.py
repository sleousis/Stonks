"""The paper soak harness (roadmap 12.10): many simulated trading days end
to end, in fast-forward, in one process.

``SoakRun`` lays out a throwaway install under a folder (both stores, the
canned market of :mod:`tests.e2e.fake_market`, active buy-and-hold and
momentum strategies on the simulated broker) and drives the real scheduler
with a fake clock that jumps from one fire to the next. The scheduler runs
the real jobs through the ``local`` backend: metadata and price ingest from
the canned source, the tick, health (which opens and clears the operational
halt) and a nightly backup.

On top of that the run injects trouble:

- **crashes**: on chosen days the tick dies mid-portfolio (a
  ``BaseException`` after the orders reach the broker, before the ledger
  commits), the scheduler process "dies" with it, a new one starts (it
  recovers the interrupted rows), and the operator reruns the day's tick;
- **a kill switch**: engaged before one day's tick and resumed after it;
- **a DST switch**: pick a window across a clock change (US DST starts on
  8 March 2026) and the ticks must keep firing once per session, at the
  same New York time.

After every tick the invariants in :func:`check_day` must hold. The run
returns a :class:`SoakReport` with the counts and every violation.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from stonks.accounts.default_book import ensure_default_subscription
from stonks.accounts.models import DEFAULT_PORTFOLIO_ID, Mode
from stonks.config import load_settings
from stonks.core.params import ParameterSpec
from stonks.core.protocols import SurvivalReport
from stonks.core.types import Order, Portfolio
from stonks.ingest.pipeline import IngestPipeline
from stonks.notify import LogNotifier
from stonks.production.halts import active_halts, clear_halt, trip_halt
from stonks.registry.store import StrategyRegistry
from stonks.scheduling.config import (
    DailyTriggerConfig,
    IntervalTriggerConfig,
    JobConfig,
    SchedulerConfig,
    SessionTriggerConfig,
)
from stonks.scheduling.jobs import build_job_specs
from stonks.scheduling.local import LocalExecutor
from stonks.scheduling.runs import RunStore
from stonks.scheduling.scheduler import Scheduler
from stonks.scheduling.triggers import Fire
from stonks.store.lake import DuckDBLake
from stonks.store.state import SqliteState
from stonks.strategies.base import BaseStrategy
from stonks.strategies.examples.buy_and_hold import BuyAndHold
from tests.e2e.fake_market import CRYPTO, EQUITIES, TICKERS, CannedDataSource, build_market

NEW_YORK = ZoneInfo("America/New_York")
PF = DEFAULT_PORTFOLIO_ID
ACTOR = "test:soak"
INITIAL_CASH = 10_000.0


class ChurnStrategy(BaseStrategy):
    """Trades every day: buys its ticker when flat, sells it all when held.
    It scores above buy-and-hold, so the default ``single_winner`` book
    trades it each tick and the ledger sees an order and a fill every day."""

    id = "soak_churn"
    hypothesis = "None: a load generator for the soak test, never a real strategy."
    label_horizon_bars = 0

    @classmethod
    def parameter_spec(cls) -> list[ParameterSpec]:
        return [
            ParameterSpec(
                name="ticker", kind="categorical", default="AAA.US", bounds=None, tunable=False
            ),
            ParameterSpec(
                name="allocation", kind="float", default=0.5, bounds=(0.0, 1.0), tunable=False
            ),
        ]

    def estimate_return(self, ticker: str, as_of: date, lake: Any) -> float | None:
        return 2.0 if ticker == self.params["ticker"] else None

    def decide(
        self,
        my_picks: Sequence[tuple[float, str]],
        portfolio: Portfolio,
        prices: Mapping[str, float],
        as_of: date,
    ) -> list[Order]:
        target = self.params["ticker"]
        held = portfolio.positions.get(target, 0.0)
        price = prices.get(target)
        if held > 0:
            side, qty = "sell", held
        elif price and price > 0 and portfolio.cash > 0:
            side, qty = "buy", portfolio.cash * float(self.params["allocation"]) / price
        else:
            return []
        return [
            Order(
                client_id=f"{self.id}:{target}:{as_of.isoformat()}",
                ticker=target,
                side=side,
                quantity=qty,
                order_type="market",
                strategy_id=self.id,
            )
        ]


class SoakCrash(BaseException):
    """A process death mid-tick: not an ``Exception``, so nothing in the
    tick or the scheduler catches it (like a SIGKILL or an OOM)."""


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.t = start

    def now(self) -> datetime:
        return self.t


@dataclass
class SoakReport:
    sessions: list[date] = field(default_factory=list)
    ticks_ok: int = 0
    orders: int = 0
    fills: int = 0
    restarts: int = 0
    backups: int = 0
    health_runs: int = 0
    halted_days: list[date] = field(default_factory=list)
    risk_rows: int = 0
    tick_local_times: set[time] = field(default_factory=set)
    violations: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{len(self.sessions)} sessions, {self.ticks_ok} ticks, {self.orders} orders,"
            f" {self.fills} fills, {self.restarts} restarts, {self.backups} backups,"
            f" {self.health_runs} health runs, {self.risk_rows} risk rows,"
            f" halted {len(self.halted_days)} days, {len(self.violations)} violations"
        )


def _config_text(root: Path) -> str:
    data = (root / "data").as_posix()
    universe = ", ".join(f'"{t}"' for t in TICKERS)
    return f"""
[lake]
path = "{data}/lake.duckdb"

[state]
path = "{data}/state.sqlite"

[registry]
artifacts_dir = "{data}/artifacts"

[logging]
level = "WARNING"

[production]
universe = [{universe}]
initial_cash = {INITIAL_CASH}

[brokers]
kind = "simulated"

[backup]
dir = "{(root / "backups").as_posix()}"

[notify]
backends = ["log", "store"]
""".lstrip()


def scheduler_config() -> SchedulerConfig:
    """The daily loop: metadata, prices, tick after the NYSE close, health
    twice a day and a nightly backup. ``poll_seconds`` is large so the fake
    clock jumps straight from fire to fire."""
    source = {"source": "canned"}
    return SchedulerConfig(
        backend="local",
        catch_up="latest",
        poll_seconds=86_400,
        deliver_notifications=False,
        jobs=[
            JobConfig(
                name="ingest_metadata",
                action="ingest_metadata",
                trigger=SessionTriggerConfig(offset_minutes=25),
                params=source,
            ),
            JobConfig(
                name="ingest_prices",
                action="ingest_prices",
                trigger=SessionTriggerConfig(offset_minutes=30),
                params=source,
            ),
            JobConfig(name="tick", action="tick", trigger=SessionTriggerConfig(offset_minutes=45)),
            JobConfig(
                name="health", action="health", trigger=IntervalTriggerConfig(every_minutes=720)
            ),
            JobConfig(name="backup", action="backup", trigger=DailyTriggerConfig(at=time(5, 0))),
        ],
    )


class SoakRun:
    """One soak over ``[start, end]`` (calendar days) under ``root``.

    ``patch(target, name, value)`` swaps a module attribute for the run
    (``monkeypatch.setattr``): the canned source and the crash hook."""

    def __init__(
        self,
        root: Path,
        start: date,
        end: date,
        patch: Callable[[Any, str, Any], None],
        *,
        crash_on: tuple[date, ...] = (),
        kill_on: date | None = None,
    ) -> None:
        self.root = Path(root)
        self.start, self.end = start, end
        self.crash_on = set(crash_on)
        self.kill_on = kill_on
        self.report = SoakReport()
        self._crashed: set[date] = set()
        self._kill_id: int | None = None
        (self.root / "config").mkdir(parents=True, exist_ok=True)
        (self.root / "data").mkdir(exist_ok=True)
        config_path = self.root / "config" / "default.toml"
        config_path.write_text(_config_text(self.root), encoding="utf-8")
        self.settings = load_settings(config_path)
        self.market = build_market(end + timedelta(days=7))
        source = CannedDataSource(self.market)

        import stonks.production.tick as tick_module
        import stonks.scheduling.local as local

        patch(local, "build_source", lambda _id, _sources: source)
        real_hooks = tick_module.run_portfolio_hooks

        def crashing_hooks(ctx: Any, *args: Any, **kwargs: Any) -> Any:
            if ctx.as_of in self.crash_on and ctx.as_of not in self._crashed:
                self._crashed.add(ctx.as_of)
                raise SoakCrash(f"process killed mid-tick on {ctx.as_of}")
            return real_hooks(ctx, *args, **kwargs)

        patch(tick_module, "run_portfolio_hooks", crashing_hooks)
        self._seed(source)

    # ---- setup ---------------------------------------------------------------------

    def _seed(self, source: CannedDataSource) -> None:
        lake = DuckDBLake(self.settings.lake.path)
        try:
            lake.migrate()
            pipeline = IngestPipeline(source, lake)
            pipeline.run_metadata(list(TICKERS))
            pipeline.run_prices(list(TICKERS), until=self.start - timedelta(days=1))
        finally:
            lake.close()
        with SqliteState(self.settings.state.path) as state:
            state.migrate()
            registry = StrategyRegistry(
                state=state, artifacts_dir=self.settings.registry.artifacts_dir
            )
            report = SurvivalReport(test_id="oos", passed=True, metrics={"sharpe_oos": 1.0})
            for ticker in (*EQUITIES, CRYPTO):
                sid = f"bah_{ticker.split('.')[0].lower()}"
                registry.register(BuyAndHold({"ticker": ticker, "allocation": 0.2}), [report], sid)
                registry.set_status(
                    sid, "active", actor=ACTOR, reason="seeded for the paper soak", override=True
                )
                # like a promotion: the default book follows every active strategy
                ensure_default_subscription(state, sid, Mode.PAPER)
            registry.register(ChurnStrategy({"ticker": "AAA.US"}), [report], "soak_churn")
            registry.set_status(
                "soak_churn",
                "active",
                actor=ACTOR,
                reason="seeded for the paper soak",
                override=True,
            )
            ensure_default_subscription(state, "soak_churn", Mode.PAPER)
        store = RunStore(self.settings.state.path)
        store.migrate()

    def _scheduler(self, clock: FakeClock) -> Scheduler:
        config = scheduler_config()
        executor = LocalExecutor()
        specs = build_job_specs(config, env={}, actions=executor.actions())
        scheduler = Scheduler(
            specs,
            RunStore(self.settings.state.path),
            settings=self.settings,
            notifier=LogNotifier(),
            config=config,
            clock=clock,
            executor=executor,
        )
        scheduler.start()
        return scheduler

    # ---- the loop ------------------------------------------------------------------

    def run(self) -> SoakReport:
        clock = FakeClock(datetime.combine(self.start, time(0, 0), tzinfo=UTC))
        scheduler = self._scheduler(clock)
        stop = datetime.combine(self.end + timedelta(days=1), time(12, 0), tzinfo=UTC)
        while True:
            fires = [s.trigger.next_fire(clock.t) for s in scheduler.specs]
            nxt = min((f.scheduled_for for f in fires if f is not None), default=None)
            if nxt is None or nxt > stop:
                break
            clock.t = nxt
            self._before(scheduler, clock)
            try:
                results = scheduler.run_pending()
            except SoakCrash:
                scheduler = self._restart(clock)
                results = [self._rerun_tick(scheduler, clock)]
            for result in results:
                self._after(result, clock)
        scheduler.stop()
        self._final_checks()
        return self.report

    def _before(self, scheduler: Scheduler, clock: FakeClock) -> None:
        """Engage the kill switch right before the kill day's tick fires."""
        due = [d for d in scheduler.due(clock.t) if d.spec.name == "tick"]
        if not due or self.kill_on is None or due[0].fire.as_of != self.kill_on:
            return
        with SqliteState(self.settings.state.path) as state:
            halt, _ = trip_halt(
                state,
                "kill",
                reason="soak: kill switch drill",
                actor=ACTOR,
                scope="global",
                halt="all",
                on=self.kill_on,
            )
        self._kill_id = halt.id

    def _restart(self, clock: FakeClock) -> Scheduler:
        """The supervisor starts a new scheduler process after the crash."""
        self.report.restarts += 1
        clock.t += timedelta(minutes=5)
        return self._scheduler(clock)

    def _rerun_tick(self, scheduler: Scheduler, clock: FakeClock) -> Any:
        """The operator reruns the crashed day's tick (``stonks schedule
        run-now tick --as-of DAY``): a manual fire key, same trading day."""
        day = max(self._crashed)
        spec = next(s for s in scheduler.specs if s.name == "tick")
        return scheduler.run_one(spec, Fire(clock.t, day, f"manual:{clock.t.isoformat()}"))

    def _after(self, result: Any, clock: FakeClock) -> None:
        if result.job_name == "backup" and result.status == "succeeded":
            self.report.backups += 1
        if result.job_name == "health":
            self.report.health_runs += 1
        if result.job_name != "tick":
            return
        day = result.fire.as_of
        if result.status != "succeeded":
            self.report.violations.append(f"{day}: tick {result.status} {result.detail}")
            return
        self.report.ticks_ok += 1
        if day not in self.report.sessions:
            self.report.sessions.append(day)
        if not result.fire.key.startswith("manual:"):
            self.report.tick_local_times.add(result.fire.scheduled_for.astimezone(NEW_YORK).time())
        halted = day == self.kill_on
        if halted:
            self.report.halted_days.append(day)
        self.report.violations += check_day(self.settings, day, halted=halted)
        if halted and self._kill_id is not None:
            with SqliteState(self.settings.state.path) as state:
                clear_halt(state, self._kill_id, actor=ACTOR, reason="soak: drill over, resume")
                assert not [h for h in active_halts(state, day) if h.kind == "kill"]
            self._kill_id = None

    def _final_checks(self) -> None:
        with SqliteState(self.settings.state.path) as state:
            self.report.orders = state.sql("SELECT COUNT(*) FROM orders")[0][0]
            self.report.fills = state.sql("SELECT COUNT(*) FROM fills")[0][0]
            self.report.risk_rows = state.sql("SELECT COUNT(*) FROM risk_snapshots")[0][0]
            ticks = state.sql(
                "SELECT COUNT(*) FROM scheduled_runs WHERE job_name = 'tick'"
                " AND status = 'succeeded'"
            )[0][0]
        if ticks < len(self.report.sessions):
            self.report.violations.append(
                f"{ticks} succeeded tick runs for {len(self.report.sessions)} sessions"
            )
        if len(self.report.tick_local_times) > 1:
            self.report.violations.append(
                f"ticks fired at different New York times: {sorted(self.report.tick_local_times)}"
            )
        backups = self.root / "backups"
        if self.report.backups and not (backups.exists() and any(backups.iterdir())):
            self.report.violations.append("backup runs succeeded but the backup folder is empty")


# ---- invariants --------------------------------------------------------------------


def _raw_closes(lake: DuckDBLake, day: date) -> dict[str, float]:
    df = lake.sql(
        "SELECT ticker, close FROM (SELECT ticker, close, row_number() OVER"
        " (PARTITION BY ticker ORDER BY date DESC) AS rn FROM prices WHERE date <= ?)"
        " WHERE rn = 1",
        [day],
    )
    return {str(r.ticker): float(r.close) for r in df.itertuples(index=False)}


def check_day(settings: Any, day: date, *, halted: bool = False) -> list[str]:
    """Every invariant after the tick for ``day``; one line per violation."""
    out: list[str] = []
    with DuckDBLake(settings.lake.path) as lake:
        closes = _raw_closes(lake, day)
        latest = lake.sql("SELECT MAX(date) AS d FROM prices WHERE ticker = ?", [EQUITIES[0]])
    last_bar = latest["d"].iloc[0]
    last = last_bar.date() if hasattr(last_bar, "date") else last_bar
    if last != day:
        out.append(f"{day}: latest {EQUITIES[0]} bar is {last}, ingest did not land the day")
    with SqliteState(settings.state.path) as state:
        out += _ledger(state, day, closes)
        out += _orders(state, day, halted)
        out += _runs(state, day)
        risk = state.sql(
            "SELECT COUNT(*) FROM risk_snapshots WHERE portfolio_id = ? AND as_of = ?"
            " AND strategy_id = ''",
            [PF, day.isoformat()],
        )[0][0]
        if risk != 1:
            out.append(f"{day}: {risk} portfolio risk snapshots (want 1)")
    return out


def _ledger(state: SqliteState, day: date, closes: dict[str, float]) -> list[str]:
    out: list[str] = []
    snaps = state.sql(
        "SELECT id, as_of, taken_at, cash, positions_json, total_value FROM portfolio_snapshots"
        " WHERE portfolio_id = ? ORDER BY id",
        [PF],
    )
    today = [s for s in snaps if s["as_of"] == day.isoformat()]
    if not today:
        return [f"{day}: no snapshot for the day"]
    snap = today[-1]
    positions = json.loads(snap["positions_json"] or "{}")
    marked = float(snap["cash"]) + sum(
        float(q) * closes.get(t, math.nan) for t, q in positions.items()
    )
    if not math.isclose(marked, float(snap["total_value"]), rel_tol=1e-6, abs_tol=1e-6):
        out.append(
            f"{day}: cash plus positions {marked:.6f} != equity {float(snap['total_value']):.6f}"
        )
    if float(snap["cash"]) < -1e-6:
        out.append(f"{day}: negative cash {snap['cash']}")
    for prev, cur in zip(snaps, snaps[1:], strict=False):
        if (cur["as_of"] or "") < (prev["as_of"] or "") or cur["taken_at"] < prev["taken_at"]:
            out.append(f"{day}: snapshot {cur['id']} goes back in time after {prev['id']}")
    # positions reconcile with the fills (no split or dividend in the window)
    net: Counter[str] = Counter()
    for r in state.sql(
        "SELECT f.ticker, f.quantity, o.side FROM fills f JOIN orders o"
        " ON o.client_id = f.order_client_id WHERE o.portfolio_id = ?",
        [PF],
    ):
        net[r["ticker"]] += float(r["quantity"]) * (1 if r["side"] == "buy" else -1)
    for ticker in set(net) | set(positions):
        if not math.isclose(net[ticker], float(positions.get(ticker, 0.0)), abs_tol=1e-6):
            out.append(
                f"{day}: {ticker} held {positions.get(ticker, 0.0)} but fills net to {net[ticker]}"
            )
    return out


def _orders(state: SqliteState, day: date, halted: bool) -> list[str]:
    out: list[str] = []
    dupes = state.sql(
        "SELECT order_client_id, quantity, price, filled_at, COUNT(*) AS n FROM fills"
        " GROUP BY order_client_id, quantity, price, filled_at HAVING n > 1"
    )
    out += [f"{day}: fill booked {r['n']} times for {r['order_client_id']}" for r in dupes]
    over = state.sql(
        "SELECT o.client_id, o.quantity, SUM(f.quantity) AS filled FROM orders o"
        " JOIN fills f ON f.order_client_id = o.client_id GROUP BY o.client_id"
        " HAVING filled > o.quantity + 1e-9"
    )
    out += [f"{day}: {r['client_id']} filled {r['filled']} of {r['quantity']}" for r in over]
    keys = Counter(
        (r["client_id"].split(":", 1)[0], r["strategy_id"], r["ticker"], r["side"])
        for r in state.sql("SELECT client_id, strategy_id, ticker, side FROM orders")
    )
    out += [f"{day}: duplicate order {k}" for k, n in keys.items() if n > 1]
    stuck = state.sql(
        "SELECT client_id FROM orders WHERE status IN ('pending', 'partially_filled')"
        " AND client_id < ?",
        [day.isoformat()],
    )
    out += [f"{day}: order {r['client_id']} still open from an earlier day" for r in stuck]
    if halted:
        placed = state.sql(
            "SELECT COUNT(*) FROM orders WHERE client_id LIKE ? AND status != 'rejected'",
            [f"{day.isoformat()}:%"],
        )[0][0]
        if placed:
            out.append(f"{day}: {placed} orders went out under the kill switch")
    return out


def _runs(state: SqliteState, day: date) -> list[str]:
    out: list[str] = []
    ticks = state.sql("SELECT id FROM tick_runs WHERE status = 'running'")
    out += [f"{day}: tick {r['id']} stuck in running" for r in ticks]
    runs = state.sql("SELECT job_name, run_key FROM scheduled_runs WHERE status = 'running'")
    out += [f"{day}: scheduled run {r['job_name']} {r['run_key']} stuck" for r in runs]
    return out
