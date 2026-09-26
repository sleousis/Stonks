"""Import hook for registered code strategies (Strategy Studio).

A registered code strategy's class path lives under the
``stonks_user_strategies`` namespace, backed by content-addressed files in
the user strategies directory that is deliberately *not* on ``sys.path``.
So a fresh process — and with it ``StrategyRegistry.load`` and the Ranker —
cannot import it.

:class:`UserStrategyFinder` is a ``sys.meta_path`` finder that resolves
exactly that namespace, and only while ``[api] allow_code_strategies`` is
on (``Services.start`` installs it, ``Services.shutdown`` removes it):

- ``stonks_user_strategies`` itself is an empty package;
- ``stonks_user_strategies.<stem>`` is ``<user_strategies_dir>/<stem>.py``,
  where ``<stem>`` must be ``[a-z0-9_]{1,80}`` and the file must already
  exist in that directory (the Studio wrote it, after its smoke check,
  when the strategy was registered).

Nothing is imported eagerly: the hook only acts when something imports a
module of that namespace.
"""

from __future__ import annotations

import contextlib
import importlib.abc
import importlib.machinery
import importlib.util
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

from stonks.logging import get_logger

USER_NAMESPACE = "stonks_user_strategies"
_STEM = re.compile(r"^[a-z0-9_]{1,80}$")

_log = get_logger("stonks.app.user_strategies")


class _EmptyPackageLoader(importlib.abc.Loader):
    def create_module(self, spec: importlib.machinery.ModuleSpec) -> ModuleType | None:
        return None

    def exec_module(self, module: ModuleType) -> None:
        return None


class UserStrategyFinder(importlib.abc.MetaPathFinder):
    def __init__(self, directory: Path) -> None:
        self._dir = Path(directory).resolve()

    def find_spec(
        self, fullname: str, path: Sequence[str] | None, target: Any = None
    ) -> importlib.machinery.ModuleSpec | None:
        if fullname == USER_NAMESPACE:
            spec = importlib.machinery.ModuleSpec(fullname, _EmptyPackageLoader(), is_package=True)
            spec.submodule_search_locations = []
            return spec
        prefix, _, stem = fullname.partition(".")
        if prefix != USER_NAMESPACE or not _STEM.match(stem):
            return None
        file = (self._dir / f"{stem}.py").resolve()
        if file.parent != self._dir or not file.is_file():
            return None
        _log.info("user_strategy.import", module=fullname)
        return importlib.util.spec_from_file_location(fullname, file)


def install(directory: Path) -> UserStrategyFinder:
    finder = UserStrategyFinder(directory)
    sys.meta_path.append(finder)
    return finder


def uninstall(finder: UserStrategyFinder) -> None:
    with contextlib.suppress(ValueError):
        sys.meta_path.remove(finder)
