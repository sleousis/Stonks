"""The journal's own rows (SQLite 049): a person's playbooks, and per trade
the playbook, whether the plan was followed, a review, tags and mistakes.

No ownership checks here: the service resolves the portfolio and the
playbook owner first (:mod:`stonks.app.journal`).
"""

from __future__ import annotations

import re
import secrets
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from stonks.store.state import SqliteState

LabelKind = Literal["tag", "mistake"]
MAX_LABEL = 40
MAX_LABELS = 20


class JournalStoreError(ValueError):
    """A write the journal refuses (a bad label, a duplicate name)."""


@dataclass(frozen=True)
class Playbook:
    id: str
    owner_id: str
    name: str
    description: str | None
    archived: bool
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Annotation:
    trade_id: int
    playbook_id: str | None
    followed_plan: bool | None
    review: str | None
    tags: tuple[str, ...]
    mistakes: tuple[str, ...]
    updated_by: str
    updated_at: str


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def normalize_labels(labels: Iterable[str]) -> tuple[str, ...]:
    """Trimmed, lower case, inner spaces collapsed, duplicates dropped, in
    the order given."""
    out: dict[str, None] = {}
    for raw in labels:
        label = re.sub(r"\s+", " ", raw).strip().lower()
        if not label:
            continue
        if len(label) > MAX_LABEL:
            raise JournalStoreError(f"a label has at most {MAX_LABEL} characters: {label!r}")
        out[label] = None
    if len(out) > MAX_LABELS:
        raise JournalStoreError(f"at most {MAX_LABELS} labels of each kind")
    return tuple(out)


# ---- annotations -------------------------------------------------------------------


def load_annotations(state: SqliteState, portfolio_id: str) -> dict[int, Annotation]:
    rows = state.sql(
        "SELECT trade_id, playbook_id, followed_plan, review, updated_by, updated_at"
        " FROM trade_annotations WHERE portfolio_id = ?",
        [portfolio_id],
    )
    labels: dict[tuple[int, str], list[str]] = {}
    for r in state.sql(
        "SELECT trade_id, kind, label FROM trade_labels WHERE portfolio_id = ?"
        " ORDER BY trade_id, kind, label",
        [portfolio_id],
    ):
        labels.setdefault((int(r["trade_id"]), r["kind"]), []).append(r["label"])
    return {
        int(r["trade_id"]): Annotation(
            trade_id=int(r["trade_id"]),
            playbook_id=r["playbook_id"],
            followed_plan=None if r["followed_plan"] is None else bool(r["followed_plan"]),
            review=r["review"],
            tags=tuple(labels.get((int(r["trade_id"]), "tag"), ())),
            mistakes=tuple(labels.get((int(r["trade_id"]), "mistake"), ())),
            updated_by=r["updated_by"],
            updated_at=r["updated_at"],
        )
        for r in rows
    }


def save_annotation(
    state: SqliteState,
    portfolio_id: str,
    trade_id: int,
    *,
    playbook_id: str | None,
    followed_plan: bool | None,
    review: str | None,
    tags: Iterable[str],
    mistakes: Iterable[str],
    actor: str,
) -> Annotation:
    """Replace a trade's annotation and labels in one transaction."""
    tag_list, mistake_list = normalize_labels(tags), normalize_labels(mistakes)
    text = review.strip() if review is not None and review.strip() else None
    now = _now()
    plan = None if followed_plan is None else int(followed_plan)
    with state.transaction():
        state.execute(
            "INSERT INTO trade_annotations (portfolio_id, trade_id, playbook_id, followed_plan,"
            " review, updated_by, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (portfolio_id, trade_id) DO UPDATE SET"
            " playbook_id = excluded.playbook_id, followed_plan = excluded.followed_plan,"
            " review = excluded.review, updated_by = excluded.updated_by,"
            " updated_at = excluded.updated_at",
            [portfolio_id, trade_id, playbook_id, plan, text, actor, now],
        )
        state.execute(
            "DELETE FROM trade_labels WHERE portfolio_id = ? AND trade_id = ?",
            [portfolio_id, trade_id],
        )
        for kind, labels in (("tag", tag_list), ("mistake", mistake_list)):
            for label in labels:
                state.execute(
                    "INSERT INTO trade_labels (portfolio_id, trade_id, kind, label)"
                    " VALUES (?, ?, ?, ?)",
                    [portfolio_id, trade_id, kind, label],
                )
    return Annotation(
        trade_id=trade_id,
        playbook_id=playbook_id,
        followed_plan=followed_plan,
        review=text,
        tags=tuple(sorted(tag_list)),
        mistakes=tuple(sorted(mistake_list)),
        updated_by=actor,
        updated_at=now,
    )


def used_labels(state: SqliteState, portfolio_id: str) -> dict[LabelKind, list[str]]:
    """Every tag and mistake used in one portfolio, for suggestions."""
    out: dict[LabelKind, list[str]] = {"tag": [], "mistake": []}
    for r in state.sql(
        "SELECT DISTINCT kind, label FROM trade_labels WHERE portfolio_id = ? ORDER BY label",
        [portfolio_id],
    ):
        out[r["kind"]].append(r["label"])
    return out


# ---- playbooks ---------------------------------------------------------------------


def _playbook(r: sqlite3.Row) -> Playbook:
    return Playbook(
        id=r["id"],
        owner_id=r["owner_id"],
        name=r["name"],
        description=r["description"],
        archived=bool(r["archived"]),
        created_at=r["created_at"],
        updated_at=r["updated_at"],
    )


def list_playbooks(
    state: SqliteState, owner_id: str, *, include_archived: bool = False
) -> list[Playbook]:
    query = "SELECT * FROM journal_playbooks WHERE owner_id = ?"
    if not include_archived:
        query += " AND archived = 0"
    return [_playbook(r) for r in state.sql(query + " ORDER BY name", [owner_id])]


def get_playbook(state: SqliteState, playbook_id: str) -> Playbook | None:
    rows = state.sql("SELECT * FROM journal_playbooks WHERE id = ?", [playbook_id])
    return _playbook(rows[0]) if rows else None


def _clean_name(name: str) -> str:
    cleaned = re.sub(r"\s+", " ", name).strip()
    if not 1 <= len(cleaned) <= 60:
        raise JournalStoreError("a playbook name has 1 to 60 characters")
    return cleaned


def _taken(state: SqliteState, owner_id: str, name: str, but: str | None = None) -> bool:
    rows = state.sql(
        "SELECT id FROM journal_playbooks WHERE owner_id = ? AND name = ? AND id IS NOT ?",
        [owner_id, name, but],
    )
    return bool(rows)


def create_playbook(
    state: SqliteState, owner_id: str, *, name: str, description: str | None
) -> Playbook:
    cleaned = _clean_name(name)
    if _taken(state, owner_id, cleaned):
        raise JournalStoreError(f"you already have a playbook named {cleaned!r}")
    pid = f"pbk_{secrets.token_hex(6)}"
    now = _now()
    state.execute(
        "INSERT INTO journal_playbooks (id, owner_id, name, description, archived, created_at,"
        " updated_at) VALUES (?, ?, ?, ?, 0, ?, ?)",
        [pid, owner_id, cleaned, (description or "").strip() or None, now, now],
    )
    found = get_playbook(state, pid)
    assert found is not None
    return found


def update_playbook(
    state: SqliteState,
    playbook: Playbook,
    *,
    name: str | None = None,
    description: str | None = None,
    archived: bool | None = None,
) -> Playbook:
    new_name = _clean_name(name) if name is not None else playbook.name
    if new_name != playbook.name and _taken(state, playbook.owner_id, new_name, playbook.id):
        raise JournalStoreError(f"you already have a playbook named {new_name!r}")
    new_description = (
        (description.strip() or None) if description is not None else playbook.description
    )
    new_archived = playbook.archived if archived is None else archived
    state.execute(
        "UPDATE journal_playbooks SET name = ?, description = ?, archived = ?, updated_at = ?"
        " WHERE id = ?",
        [new_name, new_description, int(new_archived), _now(), playbook.id],
    )
    found = get_playbook(state, playbook.id)
    assert found is not None
    return found
