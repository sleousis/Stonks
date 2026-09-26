"""Pure helpers for the guarded write tools: previews and the live-trading
check. No I/O, no MCP types."""

from __future__ import annotations

from typing import Any, Literal

LiveState = Literal["off", "on", "unknown"]

CONFIRM_HINT = "Nothing was changed. Call again with confirm=true to apply."


def live_trading_state(info: Any) -> LiveState:
    """Whether the server's broker would place real-money orders.

    Reads the ``GET /api/brokers`` payload (``BrokerInfo``: ``kind``,
    ``paper``, ``allow_live``, ``credentials_configured``). Only the
    simulated broker and Alpaca's paper endpoint count as ``"off"``; a
    non-paper Alpaca broker is ``"on"`` whatever ``allow_live`` says.
    Anything else is ``"unknown"``, and callers treat unknown as unsafe
    (fail closed).
    """
    if not isinstance(info, dict):
        return "unknown"
    kind = info.get("kind")
    if kind == "simulated":
        return "off"
    if kind == "alpaca":
        paper = info.get("paper")
        if isinstance(paper, bool):
            return "off" if paper else "on"
    return "unknown"


def status_change_preview(strategy: dict[str, Any], new_status: str) -> dict[str, Any]:
    reports = strategy.get("survival_reports") or []
    passed = [r.get("test_id") for r in reports if r.get("passed")]
    failed = [r.get("test_id") for r in reports if not r.get("passed")]
    current = strategy.get("status")
    warnings: list[str] = []
    if current == new_status:
        warnings.append(f"strategy is already {new_status}; applying is a no-op")
    if new_status == "active":
        if failed:
            warnings.append(f"failed survival tests: {', '.join(map(str, failed))}")
        if not reports:
            warnings.append("no survival reports on record")
        warnings.append("active strategies are ranked and traded by the next production tick")
    return {
        "preview": True,
        "applied": False,
        "strategy_id": strategy.get("id"),
        "class_path": strategy.get("class_path"),
        "params": strategy.get("params"),
        "current_status": current,
        "new_status": new_status,
        "survival": {"passed": passed, "failed": failed},
        "warnings": warnings,
        "next_step": CONFIRM_HINT,
    }


DraftAction = Literal["register", "enable", "disable"]

#: Registry status each Studio draft action leads to.
DRAFT_TARGET_STATUS: dict[str, str] = {
    "register": "shadow",
    "enable": "active",
    "disable": "shadow",
}

_DRAFT_FIELDS = ("id", "name", "kind", "status", "registered_strategy_id", "strategy_status")


def draft_preview(
    draft: dict[str, Any], action: DraftAction, strategy: dict[str, Any] | None
) -> dict[str, Any]:
    """Preview of registering, enabling or disabling a Studio draft.

    ``strategy`` is the registered strategy (with survival reports) for
    enable / disable, ``None`` when the draft is not registered. Source
    code is never echoed.
    """
    target = DRAFT_TARGET_STATUS[action]
    registered_as = draft.get("registered_strategy_id")
    warnings: list[str] = []
    strategy_preview: dict[str, Any] | None = None
    if action == "register":
        if registered_as:
            warnings.append(f"draft is already registered as {registered_as}; applying fails")
        warnings.append(
            "registering creates a strategy in shadow: evaluated on a virtual portfolio, "
            "never traded until enabled"
        )
    elif not registered_as:
        warnings.append("draft is not registered; register it first (applying fails)")
    elif strategy is not None:
        full = status_change_preview(strategy, target)
        warnings.extend(full["warnings"])
        strategy_preview = {
            k: full[k] for k in ("strategy_id", "class_path", "current_status", "survival")
        }
    return {
        "preview": True,
        "applied": False,
        "action": action,
        "draft": {k: draft.get(k) for k in _DRAFT_FIELDS},
        "new_status": target,
        "strategy": strategy_preview,
        "warnings": warnings,
        "next_step": CONFIRM_HINT,
    }
