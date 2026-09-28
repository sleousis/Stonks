"""Hooks the tick runs around each portfolio and after the whole tick
(BL-12, W2.1).

Hooks are found automatically: every public module of this package is
imported and each class decorated with ``@register_hook`` joins the
registry, so a new hook is one new file. Two stages:

- ``"portfolio"``: after one portfolio's orders are placed, inside the
  transaction that writes its fills and snapshot (e.g. position
  attribution). Each hook runs in its own savepoint: a failing hook is
  logged and rolled back alone, and never loses the ledger. Not run in a
  dry run.
- ``"tick"``: once per tick, after every portfolio and the model books
  (e.g. enqueueing notifications, refreshing go-live evidence). Also run in
  a dry run; hooks check ``ctx.dry_run`` themselves.

A hook may return a mapping; its keys are merged into the tick's summary
(return ``None`` to leave the summary unchanged). A hook that raises is
reported under ``hook_errors`` and the tick goes on.

Trade gates are the pre-trade counterpart: before a portfolio's orders
reach its broker, every registered :class:`TradeGate` may halt its buys
(sells and exits still go through) or all its orders, e.g. a portfolio kill
switch or a risk halt (W3.2, S6). None is registered by default.
"""

from __future__ import annotations

import importlib
import pkgutil
from abc import ABC, abstractmethod
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from stonks.logging import get_logger

if TYPE_CHECKING:
    from stonks.core.types import Fill, Order, Portfolio
    from stonks.portfolio.pipeline import PipelineResult
    from stonks.production.ranker import SignalSet
    from stonks.store.lake import DuckDBLake
    from stonks.store.state import SqliteState

__all__ = [
    "GateContext",
    "GateVerdict",
    "HookStage",
    "NotifySignal",
    "PortfolioHookContext",
    "PostTickHook",
    "TickHookContext",
    "TradeGate",
    "register_gate",
    "register_hook",
    "registered_gates",
    "registered_hooks",
    "run_gates",
    "run_portfolio_hooks",
    "run_tick_hooks",
]

_log = get_logger("stonks.production.hooks")

HookStage = Literal["portfolio", "tick"]


@dataclass(frozen=True)
class NotifySignal:
    """A signal for a notify-mode subscription: recorded by the tick and
    delivered by the notification step (S4/S6)."""

    subscription_id: str
    user_id: str
    strategy_id: str
    portfolio_id: str | None
    #: ``(ticker, raw score)``, best first.
    picks: tuple[tuple[str, float], ...]


@dataclass(frozen=True)
class PortfolioHookContext:
    state: SqliteState
    tick_id: str
    as_of: date
    portfolio_id: str
    #: The book's own positions after this tick's orders (simulated fills
    #: applied; for an external broker, the account as fetched after
    #: placing), without the owner's manual or external holdings.
    portfolio: Portfolio
    prices: Mapping[str, float]
    pipeline: PipelineResult | None
    #: ``(order, status, fill)`` per order submitted this tick.
    outcomes: Sequence[tuple[Order, str, Fill | None]]
    #: Subscription id per strategy of this book (empty for the legacy book).
    subscription_ids: Mapping[str, str] = field(default_factory=dict)
    #: The ledger tables carry ``portfolio_id`` (accounts schema present).
    scoped: bool = True


@dataclass(frozen=True)
class TickHookContext:
    state: SqliteState
    lake: DuckDBLake
    tick_id: str
    as_of: date
    dry_run: bool
    signals: SignalSet | None
    #: Per portfolio id, the summary fragment of its book.
    portfolios: Mapping[str, Mapping[str, Any]]
    notify_signals: Sequence[NotifySignal] = ()
    #: The tick's settings (read by hooks with their own settings, e.g.
    #: ``quit_rule``); ``None`` means each hook's defaults.
    settings: Any = None
    #: The tick's ``StrategyRegistry`` (for hooks that change a status).
    registry: Any = None


class PostTickHook(ABC):
    """Subclasses set ``name``, ``stage`` and ``order`` (lower runs first)
    and register with ``@register_hook``. Hooks take no constructor
    arguments."""

    name: ClassVar[str]
    stage: ClassVar[HookStage]
    order: ClassVar[int] = 100

    @abstractmethod
    def run(self, ctx: Any) -> Mapping[str, Any] | None: ...


@dataclass(frozen=True)
class GateContext:
    state: SqliteState
    as_of: date
    portfolio_id: str
    owner_id: str | None
    dry_run: bool
    #: The book's risk policy (``RiskPolicy`` with ``rules``); gates that
    #: evaluate a rule (the circuit breaker) skip it when ``None``.
    policy: Any = None
    #: The broker portfolio whose paper account this book is: halts on it
    #: (the portfolio kill switch) stop the paper account too.
    parent_portfolio_id: str | None = None


@dataclass(frozen=True)
class GateVerdict:
    #: ``"buys"`` drops every buy (sells and exits still go through);
    #: ``"all"`` places nothing.
    halt: Literal["buys", "all"]
    reason: str
    gate: str = ""


class TradeGate(ABC):
    name: ClassVar[str]
    order: ClassVar[int] = 100

    @abstractmethod
    def check(self, ctx: GateContext) -> GateVerdict | None:
        """A halt for this portfolio today, or ``None`` to let it trade."""


# ---- registries --------------------------------------------------------------------

_HOOKS: dict[str, type[PostTickHook]] = {}
_GATES: dict[str, type[TradeGate]] = {}


def _add(target: MutableMapping[str, type], cls: type) -> type:
    existing = target.get(cls.name)
    same = existing is not None and (existing.__module__, existing.__qualname__) == (
        cls.__module__,
        cls.__qualname__,
    )
    if existing is not None and not same:
        raise ValueError(
            f"{cls.name!r} is registered by both {existing.__module__}.{existing.__qualname__}"
            f" and {cls.__module__}.{cls.__qualname__}"
        )
    target[cls.name] = cls
    return cls


def register_hook(cls: type[PostTickHook]) -> type[PostTickHook]:
    """Class decorator adding a hook to the registry (names are unique)."""
    return _add(_HOOKS, cls)


def register_gate(cls: type[TradeGate]) -> type[TradeGate]:
    """Class decorator adding a trade gate to the registry."""
    return _add(_GATES, cls)


def _discover() -> None:
    import stonks.production.hooks as package

    for info in pkgutil.iter_modules(package.__path__):
        if not info.name.startswith("_"):
            importlib.import_module(f"{package.__name__}.{info.name}")


def registered_hooks(stage: HookStage) -> list[PostTickHook]:
    """One instance of every hook of ``stage``, by ``order`` then name."""
    _discover()
    classes = [c for c in _HOOKS.values() if c.stage == stage]
    return [c() for c in sorted(classes, key=lambda c: (c.order, c.name))]


def registered_gates() -> list[TradeGate]:
    _discover()
    return [c() for c in sorted(_GATES.values(), key=lambda c: (c.order, c.name))]


# ---- runners -----------------------------------------------------------------------


def run_portfolio_hooks(ctx: PortfolioHookContext, log: Any = _log) -> dict[str, Any]:
    """Run inside the caller's open transaction; one savepoint per hook."""
    out: dict[str, Any] = {}
    for hook in registered_hooks("portfolio"):
        savepoint = f"hook_{hook.name}"
        ctx.state.execute(f'SAVEPOINT "{savepoint}"')
        try:
            fragment = hook.run(ctx)
        except Exception as exc:
            ctx.state.execute(f'ROLLBACK TO "{savepoint}"')
            ctx.state.execute(f'RELEASE "{savepoint}"')
            _failed(out, hook, exc, log, portfolio_id=ctx.portfolio_id)
            continue
        ctx.state.execute(f'RELEASE "{savepoint}"')
        out.update(fragment or {})
    return out


def run_tick_hooks(ctx: TickHookContext, log: Any = _log) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for hook in registered_hooks("tick"):
        try:
            fragment = hook.run(ctx)
        except Exception as exc:
            _failed(out, hook, exc, log)
            continue
        out.update(fragment or {})
    return out


def run_gates(ctx: GateContext, log: Any = _log) -> GateVerdict | None:
    """The strictest verdict of every gate (``all`` beats ``buys``). A gate
    that raises halts buys: failing closed on new risk, open on exits."""
    verdict: GateVerdict | None = None
    for gate in registered_gates():
        try:
            v = gate.check(ctx)
        except Exception as exc:
            log.error("tick.gate_failed", gate=gate.name, error=str(exc))
            v = GateVerdict(halt="buys", reason=f"gate failed: {exc}")
        if v is None:
            continue
        v = GateVerdict(halt=v.halt, reason=v.reason, gate=v.gate or gate.name)
        if verdict is None or (v.halt == "all" and verdict.halt != "all"):
            verdict = v
    return verdict


def _failed(out: dict[str, Any], hook: PostTickHook, exc: Exception, log: Any, **kw: Any) -> None:
    log.error(
        "tick.hook_failed", hook=hook.name, error=str(exc), error_type=type(exc).__name__, **kw
    )
    out.setdefault("hook_errors", {})[hook.name] = f"{type(exc).__name__}: {exc}"
