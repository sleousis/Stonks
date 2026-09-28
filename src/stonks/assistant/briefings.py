"""Scheduled research-only briefings (roadmap 23.8).

Before the open and after the close, the assistant writes each person who
asked for it a short briefing from read tools only, and the notification
router delivers it (the feed, Web Push, email, Telegram, as the person's
preferences say). Off by default twice: ``[assistant.briefings] enabled``
for the install, and a per-person switch in ``briefing_prefs``.

Research only, never writes: the briefing runs as the person with the
``read`` scope only, under a research-only gate (no write tool is offered),
and its conversation is research only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from stonks.store.state import SqliteState

BriefingKind = Literal["pre_open", "post_close"]
KINDS: tuple[BriefingKind, ...] = ("pre_open", "post_close")

TITLES: dict[BriefingKind, str] = {
    "pre_open": "Before the open",
    "post_close": "After the close",
}

PROMPTS: dict[BriefingKind, str] = {
    "pre_open": (
        "Write my briefing before the market opens, in at most 8 short lines. Cover my "
        "portfolios, yesterday's P&L, live risk, open orders and halts, and why a ticker "
        "did or did not trade on the last tick. Use only numbers from your tools. This is "
        "research only: do not change anything."
    ),
    "post_close": (
        "Write my briefing after the market close, in at most 8 short lines. Cover today's "
        "P&L, fills, live risk, new signals and halts, and why a ticker did or did not "
        "trade today. Use only numbers from your tools. This is research only: do not "
        "change anything."
    ),
}


class BriefingSettings(BaseModel):
    """``[assistant.briefings]``: scheduled research-only briefings."""

    model_config = ConfigDict(extra="forbid")

    #: The install sends briefings at all. Each person also turns them on.
    enabled: bool = False


@dataclass(frozen=True)
class BriefingPrefs:
    pre_open: bool = False
    post_close: bool = False

    def wants(self, kind: BriefingKind) -> bool:
        return self.pre_open if kind == "pre_open" else self.post_close


def get_prefs(state: SqliteState, user_id: str) -> BriefingPrefs:
    rows = state.sql("SELECT pre_open, post_close FROM briefing_prefs WHERE user_id = ?", [user_id])
    if not rows:
        return BriefingPrefs()
    return BriefingPrefs(pre_open=bool(rows[0]["pre_open"]), post_close=bool(rows[0]["post_close"]))


def set_prefs(
    state: SqliteState, user_id: str, prefs: BriefingPrefs, *, now: datetime | None = None
) -> BriefingPrefs:
    at = (now or datetime.now(UTC)).isoformat()
    with state.transaction():
        state.execute(
            "INSERT INTO briefing_prefs (user_id, pre_open, post_close, updated_at)"
            " VALUES (?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET"
            " pre_open = excluded.pre_open, post_close = excluded.post_close,"
            " updated_at = excluded.updated_at",
            [user_id, int(prefs.pre_open), int(prefs.post_close), at],
        )
    return prefs


def subscribers(state: SqliteState, kind: BriefingKind) -> list[str]:
    """Active people who turned ``kind`` on, sorted."""
    column = "pre_open" if kind == "pre_open" else "post_close"
    rows = state.sql(
        "SELECT b.user_id FROM briefing_prefs b JOIN users u ON u.id = b.user_id"
        f" WHERE b.{column} = 1 AND u.status = 'active' AND u.kind = 'human'"
        " ORDER BY b.user_id"
    )
    return [str(r["user_id"]) for r in rows]
