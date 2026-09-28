"""Strategy names people read (UX-27).

Registered ids such as ``starter_trend`` are keys, not names. Views that
carry a strategy id also carry ``strategy_name``: the plain title the
strategy has, or ``None`` when it has none and the console builds a name
from the id (``strategyDisplayName`` in ``web/src/app/shared``). Today only
the starter set has titles.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from stonks.starter import starter_info

_NAME_HELP = "The strategy's plain title (a starter's), or null: the console names it from the id."


def strategy_title(strategy_id: str | None) -> str | None:
    """The plain title of ``strategy_id``, or ``None`` when it has none."""
    if not strategy_id:
        return None
    starter = starter_info(strategy_id)
    return starter.title if starter is not None else None


class StrategyNamed(BaseModel):
    """A view with a ``strategy_id`` field: adds ``strategy_name`` to it,
    always derived from the id (a value passed in is replaced)."""

    strategy_name: str | None = Field(default=None, description=_NAME_HELP)

    @model_validator(mode="after")
    def _derive_strategy_name(self) -> StrategyNamed:
        self.strategy_name = strategy_title(getattr(self, "strategy_id", None))
        return self
