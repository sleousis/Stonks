"""AlertService — the persisted notification feed (written by
:class:`stonks.notify.StoreNotifier`), newest first."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from stonks.app.context import AppContext
from stonks.app.pagination import Page
from stonks.notify import NotificationLevel


class AlertView(BaseModel):
    id: int
    level: NotificationLevel
    title: str
    message: str
    #: The notification's structured fields (tick id, as_of, ...), redacted.
    context: dict[str, Any]
    created_at: datetime


class AlertService:
    def __init__(self, context: AppContext) -> None:
        self._ctx = context

    def list(
        self, *, level: NotificationLevel | None = None, limit: int, offset: int
    ) -> Page[AlertView]:
        clause = " WHERE level = ?" if level else ""
        params: list[Any] = [level] if level else []
        with self._ctx.state() as state:
            total = int(state.sql(f"SELECT COUNT(*) FROM alerts{clause}", params)[0][0])
            rows = state.sql(
                f"SELECT * FROM alerts{clause} ORDER BY id DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            )
        items = [
            AlertView(
                id=r["id"],
                level=r["level"],
                title=r["title"],
                message=r["message"],
                context=json.loads(r["context_json"] or "{}"),
                created_at=datetime.fromisoformat(r["created_at"]),
            )
            for r in rows
        ]
        return Page[AlertView](items=items, total=total, limit=limit, offset=offset)
