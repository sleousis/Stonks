"""Survival-test registry and suite presets (BL-10).

Every public class in a ``stonks.lab.survival`` module that has a string
``id`` and a ``run`` method is a survival test, keyed by that ``id``. Adding
a test means adding one module; no list is edited. Modules starting with
``_`` (helpers), ``base`` and this module are skipped.

A test class may declare an ``Options`` pydantic model and a
``build(options) -> SurvivalTest`` classmethod. Without them the registry
calls the constructor with the options as keyword arguments.

``SUITE_PRESETS`` names suites by id; ``PRESET_OPTIONS`` gives a preset's
tests their options. Ids that haven't landed yet are
skipped with a logged warning, so a preset can list tests ahead of them.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from collections.abc import Mapping, Sequence
from functools import cache
from types import ModuleType
from typing import Any

from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from stonks.core.protocols import SurvivalTest
from stonks.logging import get_logger

__all__ = [
    "PRESET_OPTIONS",
    "SUITE_PRESETS",
    "build_survival_test",
    "discover_survival_tests",
    "preset_names",
    "preset_options",
    "resolve_preset",
    "resolve_suite",
    "survival_test_classes",
    "survival_test_names",
]

_log = get_logger("stonks.lab.survival.registry")

_SKIPPED_MODULES = frozenset({"base", "registry"})

#: Named suites. ``quick`` is the everyday check; ``standard`` adds
#: robustness, the deflated Sharpe, cost stress and the (informational)
#: signal IC; ``promotion`` (with the event study and the vs-random gate,
#: P5/P6) is what a
#: strategy should survive before it is registered (the default suite of
#: registering lab runs).
SUITE_PRESETS: dict[str, tuple[str, ...]] = {
    "quick": ("oos", "period_stability"),
    "standard": (
        "oos",
        "period_stability",
        "perturbation",
        "walk_forward",
        "deflated_sharpe",
        "cost_stress",
        "signal_ic",
    ),
    "promotion": (
        "oos",
        "walk_forward",
        "deflated_sharpe",
        "pbo",
        "mc_trades",
        "cost_stress",
        "plateau",
        "cross_instrument",
        "benchmark_relative",
        "mcpt",
        "event_study",
        "vs_random",
    ),
}

#: Options a preset gives its tests (``test id -> options``); a request's
#: own options for a test are applied over them. The promotion MCPT runs
#: 200 permutations and re-tunes only strategies with a non-trivial fit.
#: Its cross-instrument test adds held-out tickers from the lake so a small
#: universe still has enough names to be judged (RS-24).
PRESET_OPTIONS: dict[str, dict[str, dict[str, Any]]] = {
    "promotion": {
        "mcpt": {"n_permutations": 200, "retune": "auto"},
        "cross_instrument": {"held_out_auto": 3},
    },
}


def discover_survival_tests(package: ModuleType) -> dict[str, type]:
    """``id -> class`` for every survival test defined in ``package``'s
    public modules, sorted by id. Raises on two classes sharing an id."""
    found: dict[str, type] = {}
    for info in pkgutil.iter_modules(package.__path__):
        if info.name.startswith("_") or info.name in _SKIPPED_MODULES:
            continue
        module = importlib.import_module(f"{package.__name__}.{info.name}")
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if cls.__module__ != module.__name__ or cls.__name__.startswith("_"):
                continue
            test_id = cls.__dict__.get("id")
            if not isinstance(test_id, str) or not callable(getattr(cls, "run", None)):
                continue
            if test_id in found and found[test_id] is not cls:
                raise ValueError(
                    f"survival test id {test_id!r} is used by both "
                    f"{found[test_id].__qualname__} and {cls.__qualname__}"
                )
            found[test_id] = cls
    return dict(sorted(found.items()))


@cache
def _default_tests() -> dict[str, type]:
    import stonks.lab.survival as package

    return discover_survival_tests(package)


def survival_test_classes() -> dict[str, type]:
    """``id -> class`` for every test in ``stonks.lab.survival``."""
    return dict(_default_tests())


def survival_test_names() -> list[str]:
    """Sorted ids of every registered survival test."""
    return list(_default_tests())


def build_survival_test(
    name: str,
    options: Mapping[str, Any] | BaseModel | None = None,
    *,
    tests: Mapping[str, type] | None = None,
) -> SurvivalTest:
    """Instantiate test ``name`` with ``options``. Raises ``ValueError`` on
    an unknown name (listing the valid ones) or on options the test rejects."""
    catalog = _default_tests() if tests is None else tests
    cls = catalog.get(name)
    if cls is None:
        raise ValueError(f"unknown survival test {name!r}; choose from {sorted(catalog)}")
    raw: dict[str, Any] = (
        options.model_dump() if isinstance(options, BaseModel) else dict(options or {})
    )
    options_model = getattr(cls, "Options", None)
    try:
        if isinstance(options_model, type) and issubclass(options_model, BaseModel):
            unknown = sorted(set(raw) - set(options_model.model_fields))
            if unknown:
                raise TypeError(f"unexpected options {unknown}")
            parsed = options_model.model_validate(raw)
            build = getattr(cls, "build", None)
            if callable(build):
                return build(parsed)
            return cls(**parsed.model_dump())
        return cls(**raw)
    except (TypeError, PydanticValidationError) as exc:
        raise ValueError(f"invalid options for survival test {name!r}: {exc}") from None


def preset_names() -> list[str]:
    return sorted(SUITE_PRESETS)


def preset_options(name: str) -> dict[str, dict[str, Any]]:
    """A copy of the options preset ``name`` gives its tests (``{}`` when
    none). Raises on an unknown preset like :func:`resolve_preset`."""
    if name not in SUITE_PRESETS:
        raise ValueError(f"unknown survival preset {name!r}; choose from {preset_names()}")
    return {test: dict(opts) for test, opts in PRESET_OPTIONS.get(name, {}).items()}


def resolve_preset(name: str, *, available: Sequence[str] | None = None) -> list[str]:
    """The ids of preset ``name`` that are registered, in preset order. Ids
    not registered yet are skipped with a warning."""
    if name not in SUITE_PRESETS:
        raise ValueError(f"unknown survival preset {name!r}; choose from {preset_names()}")
    known = set(survival_test_names() if available is None else available)
    ids = SUITE_PRESETS[name]
    missing = [t for t in ids if t not in known]
    if missing:
        _log.warning("survival.preset.missing_tests", preset=name, missing=missing)
    return [t for t in ids if t in known]


def resolve_suite(
    tests: Sequence[str] | None,
    *,
    preset: str | None = None,
    default: str = "quick",
) -> list[str]:
    """Explicit ``tests`` win, then ``preset``, then the ``default`` preset."""
    if tests:
        return list(tests)
    return resolve_preset(preset or default)
