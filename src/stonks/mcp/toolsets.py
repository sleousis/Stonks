"""MCP toolsets: the tool groups an API token may use (roadmap 23.8).

A group is one tool module of :mod:`stonks.mcp.tools` (``reads``, ``jobs``,
``orders``, ``risk``, ...), so a new module is a new group with no list to
edit. A token made with ``toolsets`` sees only the tools of those groups,
plus :data:`ALWAYS`. The MCP server (``stonks mcp``) and the in-app
assistant both build their tools through
:func:`stonks.mcp.server.build_server`, which drops the rest.
"""

from __future__ import annotations

import inspect
from collections.abc import Collection, Iterable
from types import ModuleType

from stonks.mcp.tools import MODULES

#: Tools every token keeps: who the server acts as.
ALWAYS: frozenset[str] = frozenset({"whoami"})


def group_name(module: ModuleType) -> str:
    return module.__name__.rsplit(".", 1)[-1]


#: Every group, in registration order.
TOOLSET_NAMES: tuple[str, ...] = tuple(group_name(m) for m in MODULES)


def toolset_about() -> list[tuple[str, str]]:
    """``(group, one line about it)`` from each module's docstring."""
    out = []
    for module in MODULES:
        doc = inspect.getdoc(module) or ""
        first = " ".join(doc.split("\n\n", 1)[0].split())
        out.append((group_name(module), first))
    return out


def unknown_toolsets(names: Iterable[str]) -> list[str]:
    known = set(TOOLSET_NAMES)
    return sorted({n for n in names if n not in known})


def allowed(tool: str, group: str | None, toolsets: Collection[str] | None) -> bool:
    """Whether ``tool`` of ``group`` stays for a token with ``toolsets``."""
    if toolsets is None or tool in ALWAYS:
        return True
    return group is not None and group in toolsets
