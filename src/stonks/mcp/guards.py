"""Pure helpers for the guarded write tools: previews and the live-trading
check. No I/O, no MCP types."""

from __future__ import annotations

from typing import Any, Literal

LiveState = Literal["off", "on", "unknown"]

CONFIRM_HINT = "Nothing was changed. Call again with confirm=true to apply."


def live_trading_state(info: Any) -> LiveState:
    """Whether the server's broker would place real-money orders.

    Reads a broker-status payload from the API. Anything that doesn't
    state the answer unambiguously is ``"unknown"``, and callers treat
    unknown as unsafe (fail closed).
    """
    if not isinstance(info, dict):
        return "unknown"
    for key in ("live", "live_trading"):
        if key in info:
            value = info[key]
            if isinstance(value, bool):
                return "on" if value else "off"
            return "unknown"
    kind = info.get("kind")
    if kind == "simulated":
        return "off"
    if kind == "alpaca":
        paper = info.get("paper")
        if paper is True:
            return "off"
        if paper is False:
            allow_live = info.get("allow_live")
            if isinstance(allow_live, bool):
                return "on" if allow_live else "off"
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
