"""Which MCP tools the assistant may see, grouped for small models
(roadmap 20.4).

Small open-source models pick tools badly from a list of a hundred. The
assistant starts with a default set of about twenty tools and turns other
categories on when it needs them (``enable_tool_category``). The catalog
is an allowlist: a tool that is in no category is never offered, so a new
MCP tool stays out of the assistant until someone puts it here on purpose.

Placing, changing or cancelling orders directly, ticks, strategy status
changes and broker actions are in no category. With order tools on, the
assistant may only draft an order (``draft_order``), which a person
approves in the web app.

Writes that only touch research data run without asking
(:data:`RESEARCH_WRITES`). Every other write waits for the person's
confirmation in the chat.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

#: Tools every conversation starts with.
DEFAULT_TOOLS: frozenset[str] = frozenset(
    {
        "whoami",
        "get_portfolio",
        "list_portfolios",
        "get_pnl",
        "get_insights",
        "list_orders",
        "list_fills",
        "search_instruments",
        "get_bars",
        "list_watchlists",
        "list_strategies",
        "get_strategy",
        "list_halts",
        "get_live_risk",
        "list_notifications",
        "list_price_alerts",
        "list_jobs",
        "wait_for_job",
    }
)

#: Categories the assistant can turn on, with a line for the model.
CATEGORIES: dict[str, tuple[str, frozenset[str]]] = {
    "portfolio": (
        "more about your books: snapshots, agreement, trading modes, costs, cash flows",
        frozenset(
            {
                "list_portfolio_snapshots",
                "get_strategy_agreement",
                "list_trading_modes",
                "get_tca_summary",
                "list_trade_journal",
                "get_order_tca",
                "list_cash_flows",
                "get_tax_settings",
                "get_fx_rate",
            }
        ),
    ),
    "market": (
        "market data: coverage, catalog, charts, watchlists",
        frozenset({"get_coverage", "get_catalog", "get_chart", "get_watchlist", "list_sources"}),
    ),
    "strategies": (
        "strategies: history, go-live checks, leaderboard, tear sheets, shadow results",
        frozenset(
            {
                "get_strategy_history",
                "get_golive_report",
                "get_leaderboard",
                "get_tear_sheet",
                "list_subscriptions",
                "list_shadow_decisions",
                "list_shadow_pnl",
                "get_shadow_pnl",
            }
        ),
    ),
    "risk": (
        "risk: policy, your limits, daily and intraday risk snapshots, broker reconcile reports, and the kill "
        "switch (asks you first)",
        frozenset(
            {
                "get_risk_policy",
                "get_my_risk_limits",
                "list_risk_snapshots",
                "list_intraday_snapshots",
                "list_reconcile_reports",
                "get_reconcile_report",
                "engage_kill_switch",
            }
        ),
    ),
    "alerts": (
        "alerts: your feed, price alerts and when they fired, event alert switches",
        frozenset(
            {
                "list_alerts",
                "get_notification_preferences",
                "set_event_alerts",
                "list_price_alert_events",
                "create_price_alert",
                "update_price_alert",
                "delete_price_alert",
                "mark_notifications_read",
            }
        ),
    ),
    "studio": (
        "turn a plain-English idea into a rule strategy draft, validate it, backtest it",
        frozenset(
            {
                "get_rule_schema",
                "validate_rule_spec",
                "list_studio_templates",
                "get_studio_capabilities",
                "create_draft",
                "list_drafts",
                "get_draft",
                "update_draft",
                "validate_draft",
                "run_draft_backtest",
                "run_draft_lab",
            }
        ),
    ),
    "lab": (
        "the lab: backtests, lab runs, sweeps, signal studies, research sessions and results",
        frozenset(
            {
                "run_backtest",
                "run_lab",
                "run_sweep",
                "run_signal_ic",
                "get_job",
                "cancel_job",
                "list_survival_tests",
                "list_survival_presets",
                "list_cost_models",
                "list_ledger_runs",
                "get_ledger_run",
                "start_research",
                "list_research_sessions",
                "get_research_session",
            }
        ),
    ),
}

#: Order tools: offered only when the envelope turns them on.
ORDER_TOOLS: frozenset[str] = frozenset({"draft_order", "list_order_drafts"})

#: Never offered, whatever a category says.
NEVER: frozenset[str] = frozenset(
    {
        "place_order",
        "change_order",
        "cancel_order",
        "run_tick",
        "promote_strategy",
        "shadow_strategy",
        "retire_strategy",
        "swap_model_version",
        "reject_model_version",
        "sync_connection",
    }
)

#: Writes that touch only research data or the person's own feed: they run
#: without asking (still counted by the rate limit). ``draft_order`` places
#: nothing: the person approves it in the web app. Left out on purpose:
#: ``update_draft`` and ``cancel_job`` (an admin reaches other people's
#: drafts and jobs) and ``update_price_alert`` (it can switch alerts off).
RESEARCH_WRITES: frozenset[str] = frozenset(
    {
        "create_draft",
        "validate_draft",
        "run_draft_backtest",
        "run_draft_lab",
        "run_backtest",
        "run_lab",
        "run_sweep",
        "run_signal_ic",
        "start_research",
        "create_price_alert",
        "mark_notifications_read",
        "draft_order",
    }
)


def runs_without_asking(name: str, arguments: Mapping[str, Any]) -> bool:
    """Whether the write ``name`` with ``arguments`` runs at once. A code
    draft never does: its Python runs inside the API server, so a person
    reads and confirms it first."""
    if name not in RESEARCH_WRITES:
        return False
    code = arguments.get("kind") == "code" or arguments.get("source_code") is not None
    return not (name == "create_draft" and code)


#: Tools whose results name instruments a draft may use.
RESOLVERS: frozenset[str] = frozenset({"search_instruments"})

LIST_CATEGORIES = "list_tool_categories"
ENABLE_CATEGORY = "enable_tool_category"


def offered(categories: Iterable[str], *, order_tools: bool) -> frozenset[str]:
    """The tool names a conversation may see."""
    names = set(DEFAULT_TOOLS)
    for category in categories:
        entry = CATEGORIES.get(category)
        if entry is not None:
            names |= entry[1]
    if order_tools:
        names |= ORDER_TOOLS
    return frozenset(names - NEVER)
