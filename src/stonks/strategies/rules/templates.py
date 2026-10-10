"""Starter rule specs offered to the UI rule builder as templates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RuleTemplate:
    id: str
    title: str
    description: str
    spec: dict[str, Any]


def _ind(id_: str) -> dict[str, str]:
    return {"type": "indicator", "id": id_}


def _const(value: float) -> dict[str, Any]:
    return {"type": "constant", "value": value}


def _cmp(left: dict, op: str, right: dict) -> dict[str, Any]:
    return {"type": "compare", "left": left, "op": op, "right": right}


RSI_MEAN_REVERSION = RuleTemplate(
    id="rsi_mean_reversion",
    title="RSI mean reversion",
    description="Buys a stock that fell hard (RSI under 30) while still above its 50 day "
    "average. Sells when it recovers.",
    spec={
        "version": 1,
        "name": "RSI mean reversion",
        "interval": "1d",
        "universe": {"asset_classes": ["equity"]},
        "indicators": [
            {"id": "rsi14", "kind": "rsi", "period": 14},
            {"id": "sma50", "kind": "sma", "period": 50},
            {"id": "close", "kind": "close"},
        ],
        "entry": {
            "type": "all",
            "conditions": [
                _cmp(_ind("rsi14"), "<", _const(30)),
                _cmp(_ind("close"), ">", _ind("sma50")),
            ],
        },
        "exit": _cmp(_ind("rsi14"), ">", _const(55)),
        "exit_when_entry_false": False,
        "rank": {"by": "rsi14", "order": "asc"},
        "sizing": {"max_positions": 5, "allocation": 1.0},
        "risk": {"stop_loss_pct": 0.08, "take_profit_pct": None},
    },
)

SMA_TREND_FOLLOWING = RuleTemplate(
    id="sma_trend_following",
    title="SMA trend following",
    description="Holds a stock while its 20 day average is above its 50 day average. "
    "Sells when it drops back below.",
    spec={
        "version": 1,
        "name": "SMA trend following",
        "interval": "1d",
        "universe": {"asset_classes": ["equity"]},
        "indicators": [
            {"id": "sma20", "kind": "sma", "period": 20},
            {"id": "sma50", "kind": "sma", "period": 50},
            {"id": "roc20", "kind": "roc", "period": 20},
        ],
        "entry": _cmp(_ind("sma20"), ">", _ind("sma50")),
        "exit": _cmp(_ind("sma20"), "crosses_below", _ind("sma50")),
        "exit_when_entry_false": False,
        "rank": {"by": "roc20", "order": "desc"},
        "sizing": {"max_positions": 5, "allocation": 1.0},
        "risk": {"stop_loss_pct": None, "take_profit_pct": None},
    },
)

DONCHIAN_BREAKOUT = RuleTemplate(
    id="donchian_breakout",
    title="Donchian breakout",
    description="Buys when the price closes above its highest close of the last 20 days. "
    "Sells below the lowest of the last 10 days, or after a 10% fall.",
    spec={
        "version": 1,
        "name": "Donchian breakout",
        "interval": "1d",
        "universe": {"asset_classes": ["equity"]},
        "indicators": [
            {"id": "close", "kind": "close"},
            {"id": "high20", "kind": "donchian_high", "period": 20},
            {"id": "low10", "kind": "donchian_low", "period": 10},
            {"id": "roc20", "kind": "roc", "period": 20},
        ],
        "entry": _cmp(_ind("close"), "crosses_above", _ind("high20")),
        "exit": _cmp(_ind("close"), "<", _ind("low10")),
        "exit_when_entry_false": False,
        "rank": {"by": "roc20", "order": "desc"},
        "sizing": {"max_positions": 5, "allocation": 1.0},
        "risk": {"stop_loss_pct": 0.1, "take_profit_pct": None},
    },
)

TEMPLATES: dict[str, RuleTemplate] = {
    t.id: t for t in (RSI_MEAN_REVERSION, SMA_TREND_FOLLOWING, DONCHIAN_BREAKOUT)
}
