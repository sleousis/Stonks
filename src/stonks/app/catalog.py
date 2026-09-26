"""CatalogService — what can be run: strategy classes (with their parameter
specs), bar intervals and asset classes.

Strategy discovery goes through a list of :class:`StrategySource` plug-ins:
every class in a package (:class:`PackageStrategySource`, e.g. the
examples) or an explicit list of class paths
(:class:`ClassListStrategySource`, e.g. ``MacroRegimeFilter`` and the
Strategy Studio's ``RuleStrategy``). A user strategies directory is one
more source passed to :meth:`CatalogService.add_source`. The catalog is
also the allow-list for resolving a ``class_path`` sent by a client, so
nothing outside a registered source is ever imported on request.

Discovery imports modules, so its result is cached until
:meth:`CatalogService.refresh` or :meth:`CatalogService.add_source`.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
import threading
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


class ClassListStrategySource:
    """Explicitly listed ``module:Class`` paths. A path whose module does
    not exist (yet) is skipped quietly, so a source can name a class that
    another package only ships later."""

    def __init__(self, name: str, class_paths: Sequence[str]) -> None:
        self.name = name
        self.class_paths = tuple(class_paths)

    def discover(self) -> list[type]:
        found: list[type] = []
        for path in self.class_paths:
            module_name, _, cls_name = path.partition(":")
            try:
                module = importlib.import_module(module_name)
            except ModuleNotFoundError as exc:
                if exc.name is not None and module_name.startswith(exc.name):
                    _log.debug("catalog.class_not_available", class_path=path)
                else:  # the module exists but one of its imports is missing
                    _log.warning("catalog.module_import_failed", module=module_name, error=str(exc))
                continue
            except Exception as exc:
                _log.warning("catalog.module_import_failed", module=module_name, error=str(exc))
                continue
            cls = getattr(module, cls_name, None)
            if isinstance(cls, type) and _looks_like_strategy(cls):
                found.append(cls)
            else:
                _log.warning("catalog.not_a_strategy", class_path=path)
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
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[str, type]] | None = None

    def add_source(self, source: StrategySource) -> None:
        """Plug in one more source (e.g. user strategies); later sources
        never shadow a class path an earlier one already offers."""
        with self._lock:
            self._sources.append(source)
            self._cache = None

    def refresh(self) -> None:
        """Forget the discovered classes; the next call re-discovers."""
        with self._lock:
            self._cache = None

    def _classes(self) -> dict[str, tuple[str, type]]:
        with self._lock:
            if self._cache is None:
                seen: dict[str, tuple[str, type]] = {}
                for source in self._sources:
                    for cls in source.discover():
                        seen.setdefault(class_path_of(cls), (source.name, cls))
                self._cache = {k: seen[k] for k in sorted(seen)}
            return self._cache

    def strategies(self) -> list[StrategyClassInfo]:
        return [_describe(cls, source) for source, cls in self._classes().values()]

    def strategy_class(self, class_path: str) -> type:
        """Resolve a ``module:Class`` path, but only if a source offers it."""
        entry = self._classes().get(class_path)
        if entry is None:
            raise ValidationError(f"unknown strategy class {class_path!r}; see the catalog")
        return entry[1]

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
