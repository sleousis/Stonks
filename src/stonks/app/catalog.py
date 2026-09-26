"""CatalogService — what can be run: strategy classes (with their parameter
specs), bar intervals and asset classes.

Strategy discovery goes through a list of :class:`StrategySource` plug-ins.
Today the only source is the ``stonks.strategies.examples`` package; a user
strategies directory or a draft store (e.g. JSON rule specs from a strategy
studio) is one more source appended to the list. The catalog is also the
allow-list for resolving a ``class_path`` sent by a client, so nothing
outside a registered source is ever imported on request.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from collections.abc import Sequence
from typing import Any, Protocol, get_args, runtime_checkable

from pydantic import BaseModel

from stonks.app.errors import ValidationError
from stonks.app.serialize import to_jsonable
from stonks.core.interval import Interval
from stonks.core.types import AssetClass
from stonks.logging import get_logger

_log = get_logger("stonks.app.catalog")


@runtime_checkable
class StrategySource(Protocol):
    """A place strategy classes come from."""

    name: str

    def discover(self) -> list[type]: ...


class PackageStrategySource:
    """Every strategy class defined in the modules of one Python package."""

    def __init__(self, package: str, name: str | None = None) -> None:
        self._package = package
        self.name = name or package.rsplit(".", 1)[-1]

    def discover(self) -> list[type]:
        pkg = importlib.import_module(self._package)
        found: list[type] = []
        for info in pkgutil.iter_modules(pkg.__path__, prefix=f"{self._package}."):
            try:
                module = importlib.import_module(info.name)
            except Exception as exc:  # one broken module must not hide the rest
                _log.warning("catalog.module_import_failed", module=info.name, error=str(exc))
                continue
            for _, obj in inspect.getmembers(module, inspect.isclass):
                if obj.__module__ == module.__name__ and _looks_like_strategy(obj):
                    found.append(obj)
        return found


def _looks_like_strategy(cls: type) -> bool:
    return (
        not inspect.isabstract(cls)
        and not cls.__name__.startswith("_")
        and callable(getattr(cls, "parameter_spec", None))
        and callable(getattr(cls, "estimate_return", None))
        and callable(getattr(cls, "decide", None))
    )


def class_path_of(cls: type) -> str:
    return f"{cls.__module__}:{cls.__name__}"


class ParameterInfo(BaseModel):
    name: str
    kind: str
    default: Any = None
    bounds: list[Any] | None = None
    tunable: bool
    description: str


class StrategyClassInfo(BaseModel):
    class_path: str
    name: str
    source: str
    description: str
    applicable_asset_classes: list[str]
    parameters: list[ParameterInfo]


class IntervalInfo(BaseModel):
    code: str
    seconds: int
    is_intraday: bool


class CatalogService:
    def __init__(self, sources: Sequence[StrategySource]) -> None:
        self._sources = list(sources)

    def _classes(self) -> list[tuple[str, type]]:
        seen: dict[str, tuple[str, type]] = {}
        for source in self._sources:
            for cls in source.discover():
                seen.setdefault(class_path_of(cls), (source.name, cls))
        return [seen[k] for k in sorted(seen)]

    def strategies(self) -> list[StrategyClassInfo]:
        return [_describe(cls, source) for source, cls in self._classes()]

    def strategy_class(self, class_path: str) -> type:
        """Resolve a ``module:Class`` path, but only if a source offers it."""
        for _, cls in self._classes():
            if class_path_of(cls) == class_path:
                return cls
        raise ValidationError(f"unknown strategy class {class_path!r}; see the catalog")

    def intervals(self) -> list[IntervalInfo]:
        return [
            IntervalInfo(code=iv.code, seconds=iv.seconds, is_intraday=iv.is_intraday)
            for iv in Interval.STANDARD
        ]

    def asset_classes(self) -> list[str]:
        return list(get_args(AssetClass))


def _describe(cls: type, source: str) -> StrategyClassInfo:
    doc = inspect.getdoc(cls) or ""
    params = [
        ParameterInfo(
            name=spec.name,
            kind=spec.kind,
            default=to_jsonable(spec.default),
            bounds=None if spec.bounds is None else to_jsonable(list(spec.bounds)),
            tunable=spec.tunable,
            description=spec.description,
        )
        for spec in cls.parameter_spec()
    ]
    return StrategyClassInfo(
        class_path=class_path_of(cls),
        name=str(getattr(cls, "id", cls.__name__)),
        source=source,
        description=doc.split("\n\n", 1)[0].replace("\n", " "),
        applicable_asset_classes=list(getattr(cls, "applicable_asset_classes", ("equity",))),
        parameters=params,
    )
