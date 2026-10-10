"""Strategy names people read (UX-27).

Registered ids such as ``starter_trend`` are keys, not names. Views that
carry a strategy id also carry ``strategy_name``: the plain title the
strategy has, or ``None`` when it has none and the console builds a name
from the id (``strategyDisplayName`` in ``web/src/app/shared``). Today only
the starter set has titles.

Services that hold the state go one step further with :func:`name_views`:
every view then carries a name, a starter's title, the Studio draft's
name, or the id in words (``etf_trend`` reads "ETF trend"), the same rules
as the console's ``strategyDisplayName``. Signal alerts use
:func:`strategy_names` the same way, so a strategy has one name everywhere.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Any

from pydantic import BaseModel, Field, model_validator

from stonks.starter import starter_info

#: A slug plus the registry's eight hex characters (``momentum_0a1b2c3d``).
_GENERATED_ID = re.compile(r"^(.+?)_([0-9a-f]{8})$")
#: A snake_case id. A word may hold a hyphen (a ticker such as ``btc-usd``)
#: once the id has an underscore, so ``momentum-v3`` stays as it is.
_SNAKE_ID = re.compile(r"^(?:[a-z0-9]+(?:_[a-z0-9]+)*|[a-z0-9-]+(?:_[a-z0-9-]+)+)$")

#: Words that read as capitals in a name (kept in step with the console).
_ACRONYMS = {
    "etf": "ETF",
    "etfs": "ETFs",
    "rsi": "RSI",
    "sma": "SMA",
    "ema": "EMA",
    "macd": "MACD",
    "atr": "ATR",
    "ath": "ATH",
    "vwap": "VWAP",
    "orb": "ORB",
    "ewmac": "EWMAC",
    "tsmom": "TSMOM",
    "kama": "KAMA",
    "ic": "IC",
    "ml": "ML",
    "us": "US",
    "eu": "EU",
    "uk": "UK",
    "vix": "VIX",
    "hrp": "HRP",
}

_NAME_HELP = "The strategy's plain title (a starter's), or null: the console names it from the id."


def strategy_title(strategy_id: str | None) -> str | None:
    """The plain title of ``strategy_id``, or ``None`` when it has none."""
    if not strategy_id:
        return None
    starter = starter_info(strategy_id)
    return starter.title if starter is not None else None


def _humanise(snake: str) -> str:
    words = (w.upper() if "-" in w else _ACRONYMS.get(w, w) for w in snake.split("_") if w)
    text = " ".join(words)
    return text[:1].upper() + text[1:]


def strategy_display_name(strategy_id: str, *, draft_name: str | None = None) -> str:
    """The name people read: a starter's title, then the draft's name, then
    the id in words (``momentum_0a1b2c3d`` reads "Momentum 0a1b")."""
    title = strategy_title(strategy_id)
    if title:
        return title
    if draft_name and draft_name.strip():
        return draft_name.strip()
    match = _GENERATED_ID.match(strategy_id)
    if match:
        slug, digest = match.groups()
        words = _humanise(slug) if _SNAKE_ID.match(slug) else slug
        return f"{words} {digest[:4]}"
    return _humanise(strategy_id) if _SNAKE_ID.match(strategy_id) else strategy_id


def draft_refs(state: Any, strategy_ids: Iterable[str]) -> dict[str, tuple[str, str]]:
    """``(draft id, draft name)`` of each strategy registered from a Studio draft."""
    ids = sorted({i for i in strategy_ids if i})
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    return {
        str(r["registered_strategy_id"]): (str(r["id"]), str(r["name"]))
        for r in state.sql(
            "SELECT registered_strategy_id, id, name FROM strategy_drafts"
            f" WHERE registered_strategy_id IN ({marks})",
            ids,
        )
    }


def strategy_names(state: Any, strategy_ids: Iterable[str]) -> dict[str, str]:
    """A display name per id, with the names of the Studio drafts they came from."""
    ids = sorted({i for i in strategy_ids if i})
    drafts = draft_refs(state, ids)
    return {
        i: strategy_display_name(i, draft_name=drafts[i][1] if i in drafts else None) for i in ids
    }


def name_views[V: BaseModel](state: Any, views: Sequence[V]) -> Sequence[V]:
    """Set ``strategy_name`` on every view that has a ``strategy_id``."""
    names = strategy_names(state, (getattr(v, "strategy_id", None) or "" for v in views))
    for view in views:
        sid = getattr(view, "strategy_id", None)
        if sid:
            view.strategy_name = names[sid]  # type: ignore[attr-defined]
    return views


class StrategyNamed(BaseModel):
    """A view with a ``strategy_id`` field: adds ``strategy_name`` to it,
    always derived from the id (a value passed in is replaced)."""

    strategy_name: str | None = Field(default=None, description=_NAME_HELP)

    @model_validator(mode="after")
    def _derive_strategy_name(self) -> StrategyNamed:
        self.strategy_name = strategy_title(getattr(self, "strategy_id", None))
        return self
