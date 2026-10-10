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


#: What people read about each group when they limit a token (AO-02): a
#: short label and one plain sentence. The module docstrings stay as they
#: are for the MCP server and developers. A group missing here falls back to
#: its name and the docstring's first paragraph.
TOOLSET_COPY: dict[str, tuple[str, str]] = {
    "reads": ("Reads", "Look at your data. Nothing changes."),
    "jobs": ("Research jobs", "Queue backtests, lab runs and data loads. Never orders."),
    "guarded": (
        "Strategy status",
        "Change a strategy's status and start a trading run, after a confirm.",
    ),
    "model_versions": (
        "Model versions",
        "Check, retrain, swap or reject new fits, after a confirm.",
    ),
    "studio": ("Strategy Studio", "Build, test and turn on your own rule strategies."),
    "connections": (
        "Broker connections",
        "See your broker links and sync one. Linking stays in the console.",
    ),
    "halts": ("Stop trading", "See halts and stop trading. Resuming stays in the console."),
    "orders": ("Orders", "Place, change and cancel your own orders, after a confirm."),
    "execution": (
        "Execution",
        "Pick how orders are worked and plan a rebalance. Tickets still wait for you.",
    ),
    "price_alerts": ("Price alerts", "Make, change and delete your price alerts."),
    "universes": (
        "Universes",
        "Read and manage the stock lists strategies trade, after a confirm.",
    ),
    "tca": ("Trade costs", "Read trade costs and add notes to your trade journal."),
    "journal": ("Journal", "Read your round trips and their reviews."),
    "tax": ("Tax", "Read your tax settings, lot picks and FX rates."),
    "cash_flows": ("Cash flows", "Read your deposits and withdrawals."),
    "factors": ("Factors", "Read factors and queue a factor report."),
    "subscriptions": (
        "Follows",
        "See your portfolios and follows, and change a follow after a confirm.",
    ),
    "insights": ("Insights", "Read what your portfolios hold and how they did."),
    "risk": ("Risk", "Read the live risk of your portfolios."),
    "decisions": ("Trade decisions", "Read why a stock did or did not trade."),
    "notifications": (
        "Notifications",
        "Read your feed and choose which upcoming events alert you.",
    ),
    "workspace": ("Workspace", "Charts, the leaderboard, your watchlists and your risk limits."),
    "research": ("AI research", "Start a research session that only runs lab tests."),
    "calendars": ("Calendars", "Read earnings, dividends, economic releases and news."),
    "screener": ("Screener", "Run screens and keep your saved screens."),
    "screen_alerts": ("Screen alerts", "Get told when new stocks match a saved screen."),
    "live": ("Real money", "Read a real money portfolio's stage, limits and margin."),
    "options": ("Options", "Read option chains and test options strategies. Nothing trades."),
}


def toolset_about() -> list[tuple[str, str]]:
    """``(group, "Label: one plain sentence.")`` for people choosing a
    token's groups (AO-02). The label comes first, before the first colon."""
    out = []
    for module in MODULES:
        name = group_name(module)
        copy = TOOLSET_COPY.get(name)
        if copy is None:
            doc = inspect.getdoc(module) or ""
            first = " ".join(doc.split("\n\n", 1)[0].split())
            copy = (name.replace("_", " ").capitalize(), first)
        out.append((name, f"{copy[0]}: {copy[1]}"))
    return out


def unknown_toolsets(names: Iterable[str]) -> list[str]:
    known = set(TOOLSET_NAMES)
    return sorted({n for n in names if n not in known})


def allowed(tool: str, group: str | None, toolsets: Collection[str] | None) -> bool:
    """Whether ``tool`` of ``group`` stays for a token with ``toolsets``."""
    if toolsets is None or tool in ALWAYS:
        return True
    return group is not None and group in toolsets
