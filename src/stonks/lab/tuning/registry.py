"""Tuner registry: every search a lab request can name.

A tuner is one class decorated with :func:`register_tuner`. It may declare
an ``Options`` pydantic model, the settings a request's ``tuner_options``
may set (validated there, so a bad value is a 422 and never a failed job).
The registry builds it with ``build(options, seed=..., parallel=...)`` when
the class has that classmethod, else with
``cls(seed=seed, parallel=parallel, **options)``.

The modules of ``stonks.lab.tuning`` are imported to find the built-in
tuners (``grid``, ``random``, ``optuna``, ``prescreen``), so a new tuner is
one new module and no list is edited. Outside packages add tuners through
the ``stonks.tuners`` entry point group (``lab.plugins``). The first line of
a class's docstring is its label, the next paragraph its description.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
import threading
from collections.abc import Callable, Mapping
from typing import Any, TypeVar, cast

from pydantic import BaseModel, ConfigDict

from stonks.core.protocols import Tuner
from stonks.lab.parallel import ParallelSettings
from stonks.logging import get_logger

__all__ = [
    "build_tuner",
    "describe_tuner",
    "options_model",
    "register_tuner",
    "tuner_classes",
    "tuner_label",
    "tuner_names",
]

_log = get_logger("stonks.lab.tuning.registry")

_T = TypeVar("_T", bound=type)

_SKIPPED_MODULES = frozenset({"base", "registry"})

#: Tuners by name, filled by :func:`register_tuner`.
_TUNERS: dict[str, type] = {}
_discovered = False
_lock = threading.RLock()


class NoOptions(BaseModel):
    """A tuner with no settings of its own."""

    model_config = ConfigDict(extra="forbid")


def register_tuner(name: str) -> Callable[[_T], _T]:
    """Class decorator: register a tuner under ``name``. Two classes cannot
    share a name."""

    def decorate(cls: _T) -> _T:
        existing = _TUNERS.get(name)
        if existing is not None and existing is not cls:
            raise ValueError(f"tuner {name!r} is already registered by {existing.__qualname__}")
        cls.tuner_name = name  # type: ignore[attr-defined]
        _TUNERS[name] = cls
        return cls

    return decorate


def _discover() -> None:
    global _discovered
    with _lock:
        if not _discovered:
            import stonks.lab.tuning as package

            for info in pkgutil.iter_modules(package.__path__):
                if info.name.startswith("_") or info.name in _SKIPPED_MODULES:
                    continue
                importlib.import_module(f"{package.__name__}.{info.name}")
            _discovered = True
        _load_plugins()


def _load_plugins() -> None:
    from stonks.lab import plugins

    for plugin in plugins.plugin_objects(plugins.TUNERS):
        cls = plugin.obj
        if not isinstance(cls, type) or cls in _TUNERS.values():
            continue
        if plugin.name in _TUNERS:
            _log.warning("lab.plugin.name_taken", group=plugin.group, name=plugin.name)
            continue
        register_tuner(plugin.name)(cls)


def tuner_classes() -> dict[str, type]:
    """``name -> class`` for every registered tuner, sorted by name."""
    _discover()
    return dict(sorted(_TUNERS.items()))


def tuner_names() -> list[str]:
    """Sorted names of every registered tuner."""
    return list(tuner_classes())


def _tuner_class(name: str) -> type:
    classes = tuner_classes()
    try:
        return classes[name]
    except KeyError:
        raise ValueError(f"unknown tuner {name!r}; choose from {sorted(classes)}") from None


def options_model(name: str) -> type[BaseModel]:
    """The ``Options`` model of tuner ``name`` (:class:`NoOptions` if none)."""
    model = getattr(_tuner_class(name), "Options", None)
    if isinstance(model, type) and issubclass(model, BaseModel):
        return model
    return NoOptions


def validate_options(name: str, options: Mapping[str, Any] | None) -> BaseModel:
    """``options`` checked by tuner ``name``'s ``Options`` model. Raises
    pydantic's ``ValidationError`` on an unknown name or a bad value."""
    return options_model(name).model_validate(dict(options or {}))


def build_tuner(
    name: str,
    options: Mapping[str, Any] | BaseModel | None = None,
    *,
    seed: int = 0,
    parallel: ParallelSettings | None = None,
) -> Tuner:
    """A tuner ``name`` with ``options`` (validated), ``seed`` and
    ``parallel`` workers."""
    cls = _tuner_class(name)
    model = options if isinstance(options, BaseModel) else validate_options(name, options)
    build = getattr(cls, "build", None)
    if callable(build):
        return cast("Tuner", build(model, seed=seed, parallel=parallel))
    return cast("Tuner", cls(seed=seed, parallel=parallel, **model.model_dump()))


def _doc_parts(name: str) -> tuple[str, str]:
    cls = _tuner_class(name)
    doc = inspect.cleandoc(cls.__dict__.get("__doc__") or "")
    paragraphs = [" ".join(p.split()) for p in doc.split("\n\n") if p.strip()]
    label = paragraphs[0].rstrip(".") if paragraphs else name.replace("_", " ")
    return label, paragraphs[1] if len(paragraphs) > 1 else ""


def tuner_label(name: str) -> str:
    """The short label of tuner ``name`` (its docstring's first line)."""
    return _doc_parts(name)[0]


def describe_tuner(name: str) -> str:
    """One paragraph on tuner ``name`` (its docstring's second)."""
    return _doc_parts(name)[1]
