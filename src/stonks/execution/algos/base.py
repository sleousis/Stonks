"""The ``ExecutionAlgo`` seam (roadmap 23.16): how one parent order is worked.

A plain order goes out as one limit (or collared market) order. That stays
the default. An execution algo works the same parent order over time
instead, on one of two routes:

- **native**: the broker runs the algo itself. At IBKR the parent goes out
  as one order with ``algoStrategy`` and its parameters (Adaptive with a
  priority, VWAP or TWAP inside a window). IBKR's own child orders fill
  under the parent's ``orderRef``, so the ledger sees one order.
- **sliced**: the broker has no such algo, so Stonks sends child orders
  itself (``slicer.py``): whole-share slices at planned times inside the
  window, each an ordinary order with its own client id.

An algo that cannot be sliced (Adaptive) at a broker without it goes out as
a plain order (``route_for`` says ``plain``).

Every algo also names the cost it is assumed to save or add
(:class:`AlgoCostAssumption`), which the backtest's cost model applies
(``CostModelSettings.exec_algo``), so a backtest and TCA speak about the
same thing.

The algo of an order rides on ``Order.algo``, a JSON-safe mapping:
``{"name": "vwap", "params": {...}}``, plus ``"window"`` (UTC ISO start and
end) once resolved for a session, and ``"parent"`` on a child slice.
"""

from __future__ import annotations

import importlib
import math
import pkgutil
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, Literal, cast

from pydantic import BaseModel, ConfigDict, ValidationError

#: How a parent order is worked at a given broker.
AlgoRoute = Literal["native", "sliced", "plain"]


class AlgoParamsError(ValueError):
    """An algo name or parameters that do not validate."""


@dataclass(frozen=True)
class AlgoCostAssumption:
    """What the backtest assumes an algo costs, next to a plain order.

    ``spread_factor`` and ``impact_factor`` scale the cost model's half
    spread and market impact. ``timing_bps`` adds the drift of working an
    order over hours instead of at the open (it can be negative). They are
    starting points, to calibrate against TCA by algo."""

    spread_factor: float = 1.0
    impact_factor: float = 1.0
    timing_bps: float = 0.0

    def __post_init__(self) -> None:
        for name in ("spread_factor", "impact_factor"):
            value = getattr(self, name)
            if not (math.isfinite(value) and value >= 0):
                raise ValueError(f"{name} must be a finite number >= 0, got {value!r}")
        if not math.isfinite(self.timing_bps):
            raise ValueError("timing_bps must be finite")


@dataclass(frozen=True)
class AlgoWindow:
    """When an algo works: UTC start and end."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("an algo window needs time zone aware times")
        if self.end <= self.start:
            raise ValueError("an algo window must end after it starts")

    def as_dict(self) -> dict[str, str]:
        return {
            "start": self.start.astimezone(UTC).isoformat(timespec="seconds"),
            "end": self.end.astimezone(UTC).isoformat(timespec="seconds"),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> AlgoWindow:
        return cls(
            start=datetime.fromisoformat(str(raw["start"])),
            end=datetime.fromisoformat(str(raw["end"])),
        )


@dataclass(frozen=True)
class ChildSlice:
    """One child order Stonks sends: ``quantity`` whole shares, not before
    ``send_after``."""

    seq: int
    quantity: int
    send_after: datetime


@dataclass(frozen=True)
class NativeAlgo:
    """An algo in the broker's own words: IBKR's ``algoStrategy`` and its
    ``algoParams`` as tag and value strings."""

    strategy: str
    params: tuple[tuple[str, str], ...] = ()


class WindowParams(BaseModel):
    """A window inside the session: minutes after the open. ``end_minutes``
    ``None`` runs to the close."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    start_minutes: int = 0
    end_minutes: int | None = None


class ExecutionAlgo(ABC):
    """One way to work a parent order. Subclasses register with
    :func:`register_algo`; a new algo is one new module in this package."""

    name: ClassVar[str] = ""
    title: ClassVar[str] = ""
    description: ClassVar[str] = ""
    #: The parameters, validated and defaulted.
    params_model: ClassVar[type[BaseModel]]
    #: Stonks can send it as child slices at a broker without it.
    sliceable: ClassVar[bool] = False

    def validate(self, params: Mapping[str, Any] | None) -> dict[str, Any]:
        """``params`` checked and filled with defaults. Raises
        :class:`AlgoParamsError`."""
        try:
            model = self.params_model.model_validate(dict(params or {}))
        except ValidationError as exc:
            raise AlgoParamsError(f"{self.name}: {_first_error(exc)}") from None
        return model.model_dump()

    @abstractmethod
    def cost_assumption(self, params: Mapping[str, Any]) -> AlgoCostAssumption:
        """What the backtest assumes this algo costs (see the module doc)."""

    def window(
        self, params: Mapping[str, Any], session_open: datetime, session_close: datetime
    ) -> AlgoWindow | None:
        """The UTC window the algo works in for one session. ``None``: the
        algo has no window (it works until filled or the day ends)."""
        return None

    @abstractmethod
    def native(self, params: Mapping[str, Any], window: AlgoWindow | None) -> NativeAlgo:
        """The algo as IBKR names it."""

    def slices(
        self, quantity: int, params: Mapping[str, Any], window: AlgoWindow
    ) -> list[ChildSlice]:
        """The child slices Stonks sends for ``quantity`` whole shares.
        Only for :attr:`sliceable` algos."""
        raise NotImplementedError(f"{self.name} cannot be sent as slices")

    def describe(self) -> dict[str, Any]:
        """Name, title, description, whether it slices, the parameter
        schema and the default cost assumption (catalogs, the console)."""
        defaults = self.validate({})
        cost = self.cost_assumption(defaults)
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "sliceable": self.sliceable,
            "params_schema": self.params_model.model_json_schema(),
            "defaults": defaults,
            "cost_assumption": {
                "spread_factor": cost.spread_factor,
                "impact_factor": cost.impact_factor,
                "timing_bps": cost.timing_bps,
            },
        }


def _first_error(exc: ValidationError) -> str:
    err = exc.errors()[0]
    where = ".".join(str(p) for p in err.get("loc", ())) or "params"
    return f"{where}: {err.get('msg', 'invalid')}"


# ---- windows and slices, shared by the algos ---------------------------------------


def session_window(
    params: WindowParams | Mapping[str, Any], session_open: datetime, session_close: datetime
) -> AlgoWindow:
    """The window ``start_minutes`` to ``end_minutes`` after the open,
    clipped to the session. Raises :class:`AlgoParamsError` when it is
    empty."""
    p = (
        params
        if isinstance(params, WindowParams)
        else WindowParams.model_validate(
            {k: params.get(k) for k in ("start_minutes", "end_minutes") if k in params}
        )
    )
    start = session_open + timedelta(minutes=max(0, p.start_minutes))
    end = (
        session_close
        if p.end_minutes is None
        else min(session_close, session_open + timedelta(minutes=p.end_minutes))
    )
    if end <= start:
        raise AlgoParamsError("the algo window is empty inside the session")
    return AlgoWindow(start=start.astimezone(UTC), end=end.astimezone(UTC))


def split_whole(quantity: int, weights: Sequence[float]) -> list[int]:
    """``quantity`` whole shares split by ``weights`` (largest remainder),
    summing to ``quantity`` exactly."""
    if quantity < 0:
        raise ValueError("quantity must be >= 0")
    total = sum(weights)
    if not weights or total <= 0:
        raise ValueError("weights must be positive")
    raw = [quantity * w / total for w in weights]
    parts = [math.floor(r) for r in raw]
    left = quantity - sum(parts)
    order = sorted(range(len(raw)), key=lambda i: (-(raw[i] - parts[i]), i))
    for i in order[:left]:
        parts[i] += 1
    return parts


def slices_over(quantity: int, window: AlgoWindow, weights: Sequence[float]) -> list[ChildSlice]:
    """One slice per weight, evenly spaced from the window start, empty
    slices dropped and the rest renumbered from 1."""
    parts = split_whole(quantity, weights)
    step = (window.end - window.start) / len(parts)
    out: list[ChildSlice] = []
    for i, qty in enumerate(parts):
        if qty <= 0:
            continue
        out.append(ChildSlice(seq=len(out) + 1, quantity=qty, send_after=window.start + step * i))
    return out


def ib_time(when: datetime) -> str:
    """A time as IBKR's algo parameters read it: ``YYYYMMDD-HH:MM:SS`` in UTC."""
    return when.astimezone(UTC).strftime("%Y%m%d-%H:%M:%S")


# ---- the registry ------------------------------------------------------------------

_REGISTRY: dict[str, ExecutionAlgo] = {}
_discovered: set[str] = set()


def register_algo(cls: type[ExecutionAlgo]) -> type[ExecutionAlgo]:
    """Class decorator: register one algo under its ``name``."""
    if not cls.name:
        raise ValueError(f"{cls.__qualname__} needs a name")
    existing = _REGISTRY.get(cls.name)
    if existing is not None and type(existing).__qualname__ != cls.__qualname__:
        raise ValueError(f"execution algo {cls.name!r} is already registered")
    _REGISTRY[cls.name] = cls()
    return cls


def discover_algos() -> None:
    """Import every public module of this package once, so their
    ``@register_algo`` decorators run."""
    if _discovered:
        return
    pkg = importlib.import_module("stonks.execution.algos")
    for info in pkgutil.iter_modules(pkg.__path__):
        if not info.name.startswith("_") and info.name not in _NOT_ALGOS:
            importlib.import_module(f"stonks.execution.algos.{info.name}")
    _discovered.add("stonks.execution.algos")


#: Modules of the package that hold no algo.
_NOT_ALGOS = frozenset({"base", "settings"})


def algo_names() -> list[str]:
    discover_algos()
    return sorted(_REGISTRY)


def get_algo(name: str) -> ExecutionAlgo:
    """The algo registered as ``name``. Raises :class:`AlgoParamsError`."""
    discover_algos()
    algo = _REGISTRY.get(name)
    if algo is None:
        raise AlgoParamsError(f"unknown execution algo {name!r}; one of {sorted(_REGISTRY)}")
    return algo


# ---- the algo on an order ------------------------------------------------------------


def algo_spec(name: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The JSON-safe ``Order.algo`` mapping for ``name``, parameters checked."""
    algo = get_algo(name)
    return {"name": algo.name, "params": algo.validate(params)}


def algo_name_of(spec: Mapping[str, Any] | None) -> str | None:
    """The algo name in an ``Order.algo`` mapping, ``None`` for a plain order."""
    if not spec:
        return None
    name = spec.get("name")
    return str(name) if name else None


def with_window(
    spec: Mapping[str, Any], session_open: datetime, session_close: datetime
) -> dict[str, Any]:
    """``spec`` with its window resolved for one session (unchanged when
    the algo has none)."""
    algo = get_algo(str(spec["name"]))
    params = algo.validate(spec.get("params"))
    window = algo.window(params, session_open, session_close)
    out = {**dict(spec), "params": params}
    if window is not None:
        out["window"] = window.as_dict()
    return out


def window_of(spec: Mapping[str, Any]) -> AlgoWindow | None:
    raw: object = spec.get("window")
    if not isinstance(raw, Mapping):
        return None
    return AlgoWindow.from_dict(cast("Mapping[str, Any]", raw))


def native_of(spec: Mapping[str, Any]) -> NativeAlgo:
    """The broker form of a resolved ``Order.algo`` mapping."""
    algo = get_algo(str(spec["name"]))
    params = algo.validate(spec.get("params"))
    return algo.native(params, window_of(spec))


def cost_assumption_of(name: str, params: Mapping[str, Any] | None = None) -> AlgoCostAssumption:
    algo = get_algo(name)
    return algo.cost_assumption(algo.validate(params))


def route_for(name: str, native_algos: frozenset[str] | set[str]) -> AlgoRoute:
    """How ``name`` is worked at a broker that runs ``native_algos``."""
    if name in native_algos:
        return "native"
    return "sliced" if get_algo(name).sliceable else "plain"
