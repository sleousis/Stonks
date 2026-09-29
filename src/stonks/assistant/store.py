"""Conversations, messages and pending write actions per user (migration 027).

Every read and write names the owner: another user's conversation reads as
missing (:class:`ConversationNotFound`), so ids never leak. Each call opens
its own state connection through ``state_factory``.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from stonks.assistant.model import ChatMessage, Role, ToolCall
from stonks.store.state import SqliteState

StateFactory = Callable[[], AbstractContextManager[SqliteState]]
#: Checks one more write inside a write transaction; returns why it is
#: refused (a burst), or None.
WriteCheck = Callable[[SqliteState], "str | None"]
ActionStatus = Literal["pending", "approved", "rejected", "done", "failed"]

TITLE_MAX = 80


class ConversationNotFound(LookupError):
    """No such conversation, or it belongs to someone else."""


class ActionNotFound(LookupError):
    """No such pending action in this conversation."""


#: The ``audit_log`` action of each write the assistant ran or proposed.
WRITE_AUDIT_ACTION = "assistant.write"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Conversation:
    id: str
    owner_id: str
    title: str
    created_at: str
    updated_at: str
    research_only: bool = False
    tool_categories: tuple[str, ...] = ()


@dataclass(frozen=True)
class StoredMessage:
    id: int
    role: Role
    content: str
    tool_calls: tuple[ToolCall, ...]
    tool_call_id: str | None
    tool_name: str | None
    created_at: str

    def chat(self) -> ChatMessage:
        return ChatMessage(
            role=self.role,
            content=self.content,
            tool_calls=self.tool_calls,
            tool_call_id=self.tool_call_id,
            name=self.tool_name,
        )


@dataclass(frozen=True)
class PendingAction:
    id: str
    conversation_id: str
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]
    status: ActionStatus
    result: Any
    created_at: str
    decided_at: str | None


def _calls_json(calls: tuple[ToolCall, ...]) -> str | None:
    if not calls:
        return None
    return json.dumps([{"id": c.id, "name": c.name, "arguments": c.arguments} for c in calls])


def _calls(raw: str | None) -> tuple[ToolCall, ...]:
    if not raw:
        return ()
    return tuple(
        ToolCall(c["id"], c["name"], dict(c.get("arguments") or {})) for c in json.loads(raw)
    )


class ConversationStore:
    def __init__(self, state_factory: StateFactory) -> None:
        self._state = state_factory

    def _open(self) -> AbstractContextManager[SqliteState]:
        return self._state()

    # ---- conversations ----------------------------------------------------

    def create(
        self, owner_id: str, title: str = "", *, research_only: bool = False
    ) -> Conversation:
        cid = f"cnv_{secrets.token_hex(8)}"
        now = _now()
        with self._open() as state:
            state.execute(
                "INSERT INTO assistant_conversations (id, owner_id, title, research_only,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                [cid, owner_id, title.strip()[:TITLE_MAX], int(research_only), now, now],
            )
        return Conversation(cid, owner_id, title.strip()[:TITLE_MAX], now, now, research_only)

    def enable_category(self, conversation_id: str, category: str) -> tuple[str, ...]:
        """Turn a tool category on for the rest of the conversation."""
        with self._open() as state, state.transaction():
            row = state.sql(
                "SELECT tool_categories_json FROM assistant_conversations WHERE id = ?",
                [conversation_id],
            )
            current = list(json.loads(row[0]["tool_categories_json"] or "[]")) if row else []
            if category not in current:
                current.append(category)
            state.execute(
                "UPDATE assistant_conversations SET tool_categories_json = ? WHERE id = ?",
                [json.dumps(current), conversation_id],
            )
        return tuple(current)

    def categories(self, conversation_id: str) -> tuple[str, ...]:
        with self._open() as state:
            row = state.sql(
                "SELECT tool_categories_json FROM assistant_conversations WHERE id = ?",
                [conversation_id],
            )
        return tuple(json.loads(row[0]["tool_categories_json"] or "[]")) if row else ()

    # ---- turns (the trace) -------------------------------------------------------

    def start_turn(
        self, conversation_id: str, owner_id: str, model: str, prompt_version: str
    ) -> str:
        tid = f"trn_{secrets.token_hex(8)}"
        with self._open() as state:
            state.execute(
                "INSERT INTO assistant_turns (id, conversation_id, owner_id, model,"
                " prompt_version, status, started_at) VALUES (?, ?, ?, ?, ?, 'running', ?)",
                [tid, conversation_id, owner_id, model, prompt_version, _now()],
            )
        return tid

    def finish_turn(
        self,
        turn_id: str,
        *,
        status: str,
        steps: int,
        trace: list[dict[str, Any]],
        draft_ids: list[str],
    ) -> None:
        with self._open() as state:
            state.execute(
                "UPDATE assistant_turns SET status = ?, steps = ?, trace_json = ?,"
                " draft_ids_json = ?, finished_at = ? WHERE id = ?",
                [
                    status,
                    steps,
                    json.dumps(trace, default=str),
                    json.dumps(draft_ids),
                    _now(),
                    turn_id,
                ],
            )

    def turns(self, conversation_id: str) -> list[dict[str, Any]]:
        with self._open() as state:
            rows = state.sql(
                "SELECT * FROM assistant_turns WHERE conversation_id = ? ORDER BY started_at, id",
                [conversation_id],
            )
        return [
            {
                "id": r["id"],
                "model": r["model"],
                "prompt_version": r["prompt_version"],
                "status": r["status"],
                "steps": int(r["steps"]),
                "trace": json.loads(r["trace_json"] or "[]"),
                "draft_ids": json.loads(r["draft_ids_json"] or "[]"),
                "started_at": r["started_at"],
                "finished_at": r["finished_at"],
            }
            for r in rows
        ]

    def list(self, owner_id: str, *, limit: int, offset: int) -> tuple[list[Conversation], int]:
        with self._open() as state:
            total = int(
                state.sql(
                    "SELECT COUNT(*) FROM assistant_conversations WHERE owner_id = ?", [owner_id]
                )[0][0]
            )
            rows = state.sql(
                "SELECT * FROM assistant_conversations WHERE owner_id = ?"
                " ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
                [owner_id, limit, offset],
            )
        return [self._conversation(r) for r in rows], total

    def get(self, owner_id: str, conversation_id: str) -> Conversation:
        with self._open() as state:
            rows = state.sql(
                "SELECT * FROM assistant_conversations WHERE id = ? AND owner_id = ?",
                [conversation_id, owner_id],
            )
        if not rows:
            raise ConversationNotFound(f"no conversation {conversation_id!r}")
        return self._conversation(rows[0])

    def delete(self, owner_id: str, conversation_id: str) -> None:
        self.get(owner_id, conversation_id)
        with self._open() as state, state.transaction():
            state.execute(
                "DELETE FROM assistant_pending_actions WHERE conversation_id = ?",
                [conversation_id],
            )
            state.execute(
                "DELETE FROM assistant_messages WHERE conversation_id = ?", [conversation_id]
            )
            state.execute(
                "DELETE FROM assistant_turns WHERE conversation_id = ?", [conversation_id]
            )
            state.execute(
                "DELETE FROM assistant_conversations WHERE id = ? AND owner_id = ?",
                [conversation_id, owner_id],
            )

    def set_title_if_empty(self, conversation_id: str, title: str) -> None:
        with self._open() as state:
            state.execute(
                "UPDATE assistant_conversations SET title = ? WHERE id = ? AND title = ''",
                [title.strip()[:TITLE_MAX], conversation_id],
            )

    @staticmethod
    def _conversation(row: Any) -> Conversation:
        return Conversation(
            id=row["id"],
            owner_id=row["owner_id"],
            title=row["title"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            research_only=bool(row["research_only"]),
            tool_categories=tuple(json.loads(row["tool_categories_json"] or "[]")),
        )

    # ---- messages ------------------------------------------------------------

    def add_message(self, conversation_id: str, message: ChatMessage) -> int:
        now = _now()
        with self._open() as state, state.transaction():
            cur = state.execute(
                "INSERT INTO assistant_messages (conversation_id, role, content, tool_calls_json,"
                " tool_call_id, tool_name, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    conversation_id,
                    message.role,
                    message.content,
                    _calls_json(message.tool_calls),
                    message.tool_call_id,
                    message.name,
                    now,
                ],
            )
            state.execute(
                "UPDATE assistant_conversations SET updated_at = ? WHERE id = ?",
                [now, conversation_id],
            )
        assert cur.lastrowid is not None
        return int(cur.lastrowid)

    def messages(self, conversation_id: str, *, last: int | None = None) -> list[StoredMessage]:
        with self._open() as state:
            if last is None:
                rows = state.sql(
                    "SELECT * FROM assistant_messages WHERE conversation_id = ? ORDER BY id",
                    [conversation_id],
                )
            else:
                rows = state.sql(
                    "SELECT * FROM (SELECT * FROM assistant_messages WHERE conversation_id = ?"
                    " ORDER BY id DESC LIMIT ?) ORDER BY id",
                    [conversation_id, last],
                )
        return [
            StoredMessage(
                id=int(r["id"]),
                role=r["role"],
                content=r["content"],
                tool_calls=_calls(r["tool_calls_json"]),
                tool_call_id=r["tool_call_id"],
                tool_name=r["tool_name"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    # ---- pending actions ----------------------------------------------------------

    def add_action(
        self, conversation_id: str, tool_call_id: str, tool_name: str, arguments: dict[str, Any]
    ) -> PendingAction:
        action, _ = self.reserve_action(conversation_id, tool_call_id, tool_name, arguments)
        assert action is not None
        return action

    def run_check(self, check: WriteCheck) -> str | None:
        """``check`` inside a write transaction (it may write a freeze)."""
        with self._open() as state, state.transaction():
            return check(state)

    def reserve_action(
        self,
        conversation_id: str,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        check: WriteCheck | None = None,
    ) -> tuple[PendingAction | None, str | None]:
        """``(action, None)``, or ``(None, refusal)`` when ``check`` refuses.
        The check and the new row share one write transaction (``BEGIN
        IMMEDIATE``), so a parallel turn waits and then counts this row."""
        aid = f"act_{secrets.token_hex(8)}"
        now = _now()
        with self._open() as state, state.transaction():
            refusal = check(state) if check is not None else None
            if refusal is not None:
                return None, refusal
            state.execute(
                "INSERT INTO assistant_pending_actions (id, conversation_id, tool_call_id,"
                " tool_name, arguments_json, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    aid,
                    conversation_id,
                    tool_call_id,
                    tool_name,
                    json.dumps(arguments),
                    "pending",
                    now,
                ],
            )
            # The write rate limit counts these audit rows, which outlive the
            # conversation (deleting it must not reset the limit).
            state.execute(
                "INSERT INTO audit_log (actor, action, target_kind, target_id, details_json,"
                " created_at) SELECT 'user:' || owner_id, ?, 'assistant_action', ?, ?, ?"
                " FROM assistant_conversations WHERE id = ?",
                [
                    WRITE_AUDIT_ACTION,
                    aid,
                    json.dumps({"tool": tool_name, "conversation_id": conversation_id}),
                    now,
                    conversation_id,
                ],
            )
        action = PendingAction(
            aid, conversation_id, tool_call_id, tool_name, arguments, "pending", None, now, None
        )
        return action, None

    def action(self, conversation_id: str, action_id: str) -> PendingAction:
        with self._open() as state:
            rows = state.sql(
                "SELECT * FROM assistant_pending_actions WHERE id = ? AND conversation_id = ?",
                [action_id, conversation_id],
            )
        if not rows:
            raise ActionNotFound(f"no action {action_id!r}")
        return self._action(rows[0])

    def pending(self, conversation_id: str) -> list[PendingAction]:
        with self._open() as state:
            rows = state.sql(
                "SELECT * FROM assistant_pending_actions WHERE conversation_id = ?"
                " AND status = 'pending' ORDER BY created_at, id",
                [conversation_id],
            )
        return [self._action(r) for r in rows]

    def claim(self, action_id: str, status: ActionStatus) -> bool:
        """Move a pending action to ``status``; False when it was no longer
        pending (a second click, or a parallel request, loses)."""
        with self._open() as state:
            cur = state.execute(
                "UPDATE assistant_pending_actions SET status = ?, decided_at = ?"
                " WHERE id = ? AND status = 'pending'",
                [status, _now(), action_id],
            )
        return cur.rowcount == 1

    def finish(self, action_id: str, status: ActionStatus, result: Any) -> None:
        with self._open() as state:
            state.execute(
                "UPDATE assistant_pending_actions SET status = ?, result_json = ? WHERE id = ?",
                [status, json.dumps(result, default=str), action_id],
            )

    @staticmethod
    def _action(row: Any) -> PendingAction:
        return PendingAction(
            id=row["id"],
            conversation_id=row["conversation_id"],
            tool_call_id=row["tool_call_id"],
            tool_name=row["tool_name"],
            arguments=json.loads(row["arguments_json"]),
            status=row["status"],
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            created_at=row["created_at"],
            decided_at=row["decided_at"],
        )
