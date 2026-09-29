"""The stored signal phase (roadmap 15.5, S5; design section 5).

After the model books advance, the tick records what every scored strategy
said today, once for everyone:

- ``signals``: one row per strategy and ticker it scored or its model book
  holds: the score, its rank and the ticker's weight in the model book;
- ``signal_events``: what changed, ``entry``, ``exit``, ``increase`` or
  ``decrease``, each with a plain reason.

A strategy with a model book (``shadow_portfolio_snapshots`` for today)
signals what its book did: bought (entry), sold out (exit), added
(increase) or trimmed (decrease). A strategy without one signals changes in
its scored set against its last recorded day: a new ticker is an entry, a
dropped one an exit.

**Reasons.** A strategy may define ``explain(ticker, as_of, lake) ->
Mapping[str, Any]``; its keys are merged over the default reason (score,
rank, model weights, a one-line ``text``). A failing ``explain`` keeps the
default and records ``explain_error``.

The phase is keyed by ``(strategy_id, as_of)``: a strategy already recorded
for the day is skipped, so a re-run writes nothing twice. A scored day with
nothing to record (no picks, no model book holding) still writes one marker
row (ticker ``""``, no score), so the next day diffs against that empty day
and an exit is signalled once, not every day (BE-17).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Literal

from stonks.logging import get_logger
from stonks.production.prices import load_prices
from stonks.store.lake import DuckDBLake
from stonks.store.state import SqliteState

_log = get_logger("stonks.production.signals")

EventKind = Literal["entry", "exit", "increase", "decrease", "risk"]

_QTY_EPSILON = 1e-9

#: ``signals.ticker`` of the marker row of a scored day with no picks.
NO_PICKS = ""


@dataclass(frozen=True)
class SignalEvent:
    strategy_id: str
    ticker: str
    kind: EventKind
    strength: float | None
    reason: Mapping[str, Any]

    @property
    def text(self) -> str:
        return str(self.reason.get("text", ""))


def signals_recorded(state: SqliteState) -> bool:
    return bool(state.sql("SELECT 1 FROM sqlite_master WHERE type='table' AND name='signals'"))


def record_signals(
    state: SqliteState,
    lake: DuckDBLake,
    scores: Mapping[str, Mapping[str, float]],
    instances: Mapping[str, Any],
    *,
    tick_id: str,
    as_of: date,
    max_price_staleness_days: int = 7,
) -> dict[str, list[SignalEvent]]:
    """Record the day's signals and events of every strategy in ``scores``
    not recorded yet for ``as_of``. Returns the new events per strategy."""
    day = as_of.isoformat()
    done = {
        r["strategy_id"]
        for r in state.sql("SELECT DISTINCT strategy_id FROM signals WHERE as_of = ?", [day])
    }
    todo = [sid for sid in scores if sid not in done]
    if not todo:
        return {}
    books = {sid: _model_book(state, sid, day) for sid in todo}
    held = sorted({t for b in books.values() if b for t in (*b[0], *b[1])})
    prices = (
        load_prices(lake, [], held, as_of, max_staleness_days=max_price_staleness_days).prices
        if held
        else {}
    )
    now = datetime.now(UTC).isoformat(timespec="seconds")
    out: dict[str, list[SignalEvent]] = {}
    with state.transaction():
        for sid in todo:
            events = _record_one(
                state,
                lake,
                sid,
                scores[sid],
                instances.get(sid),
                books[sid],
                prices,
                tick_id=tick_id,
                as_of=as_of,
                now=now,
            )
            out[sid] = events
    _log.info("tick.signals_recorded", tick_id=tick_id, strategies=len(todo),
              events=sum(len(e) for e in out.values()))  # fmt: skip
    return out


def events_for(state: SqliteState, as_of: date, strategy_ids: Sequence[str]) -> list[SignalEvent]:
    """The stored events of ``as_of`` for these strategies, strongest first."""
    if not strategy_ids:
        return []
    marks = ", ".join("?" for _ in strategy_ids)
    rows = state.sql(
        "SELECT strategy_id, ticker, kind, strength, reason_json FROM signal_events"
        f" WHERE as_of = ? AND strategy_id IN ({marks})"
        " ORDER BY strategy_id, ABS(COALESCE(strength, 0)) DESC, id",
        [as_of.isoformat(), *strategy_ids],
    )
    return [
        SignalEvent(
            r["strategy_id"], r["ticker"], r["kind"], r["strength"], json.loads(r["reason_json"])
        )
        for r in rows
    ]


def strategies_with_signals(state: SqliteState, as_of: date) -> set[str]:
    rows = state.sql(
        "SELECT DISTINCT strategy_id FROM signals WHERE as_of = ?", [as_of.isoformat()]
    )
    return {r["strategy_id"] for r in rows}


# ---- internals --------------------------------------------------------------------


#: ``(positions today, positions before, total value today)`` of a model book.
_Book = tuple[dict[str, float], dict[str, float], float]


def _model_book(state: SqliteState, strategy_id: str, day: str) -> _Book | None:
    """The model book's positions today and on its previous day, or None
    when it has no snapshot for today (no model book, or it failed)."""
    rows = state.sql(
        "SELECT positions_json, total_value FROM shadow_portfolio_snapshots"
        " WHERE strategy_id = ? AND as_of = ? ORDER BY id DESC LIMIT 1",
        [strategy_id, day],
    )
    if not rows:
        return None
    prev = state.sql(
        "SELECT positions_json FROM shadow_portfolio_snapshots"
        " WHERE strategy_id = ? AND as_of < ? ORDER BY as_of DESC, id DESC LIMIT 1",
        [strategy_id, day],
    )
    before = json.loads(prev[0]["positions_json"]) if prev else {}
    return json.loads(rows[0]["positions_json"]), before, float(rows[0]["total_value"])


def _weight(qty: float, price: float | None, total: float) -> float | None:
    if price is None or total <= 0:
        return None
    return qty * price / total


def _record_one(
    state: SqliteState,
    lake: DuckDBLake,
    sid: str,
    scores: Mapping[str, float],
    strategy: Any,
    book: _Book | None,
    prices: Mapping[str, float],
    *,
    tick_id: str,
    as_of: date,
    now: str,
) -> list[SignalEvent]:
    day = as_of.isoformat()
    ranked = sorted(scores, key=lambda t: scores[t], reverse=True)
    rank = {t: i + 1 for i, t in enumerate(ranked)}
    after_w: dict[str, float | None] = {}
    before_w: dict[str, float | None] = {}
    changes: list[tuple[str, EventKind]] = []
    if book is not None:
        today, before, total = book
        for t in sorted({*today, *before}):
            q1, q0 = float(today.get(t, 0.0)), float(before.get(t, 0.0))
            after_w[t] = _weight(q1, prices.get(t), total)
            before_w[t] = _weight(q0, prices.get(t), total)
            if q1 > _QTY_EPSILON and q0 <= _QTY_EPSILON:
                changes.append((t, "entry"))
            elif q0 > _QTY_EPSILON and q1 <= _QTY_EPSILON:
                changes.append((t, "exit"))
            elif q1 > q0 + _QTY_EPSILON:
                changes.append((t, "increase"))
            elif q1 < q0 - _QTY_EPSILON:
                changes.append((t, "decrease"))
        tickers = list(dict.fromkeys([*scores, *today]))
    else:
        prev = state.sql(
            "SELECT ticker FROM signals WHERE strategy_id = ? AND score IS NOT NULL"
            " AND ticker != '' AND as_of ="
            " (SELECT MAX(as_of) FROM signals WHERE strategy_id = ? AND as_of < ?)",
            [sid, sid, day],
        )
        previous = {r["ticker"] for r in prev}
        entries: list[tuple[str, EventKind]] = [(t, "entry") for t in scores if t not in previous]
        exits: list[tuple[str, EventKind]] = [
            (t, "exit") for t in sorted(previous) if t not in scores
        ]
        changes = entries + exits
        tickers = list(scores)
    if not tickers:
        # the marker of a scored day with no picks (BE-17)
        state.execute(
            "INSERT OR IGNORE INTO signals (as_of, strategy_id, ticker, tick_id, score, rank,"
            " model_weight) VALUES (?, ?, ?, ?, NULL, NULL, NULL)",
            [day, sid, NO_PICKS, tick_id],
        )
    for t in tickers:
        state.execute(
            "INSERT OR IGNORE INTO signals (as_of, strategy_id, ticker, tick_id, score, rank,"
            " model_weight) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                day,
                sid,
                t,
                tick_id,
                scores.get(t),
                rank.get(t),
                (after_w.get(t) or 0.0) if book is not None else None,
            ],
        )
    events: list[SignalEvent] = []
    for ticker, kind in changes:
        reason = _reason(
            strategy,
            lake,
            sid,
            ticker,
            kind,
            as_of,
            score=scores.get(ticker),
            rank=rank.get(ticker),
            of=len(ranked),
            before=before_w.get(ticker),
            after=after_w.get(ticker),
            model_book=book is not None,
        )
        if book is not None:
            strength = (after_w.get(ticker) or 0.0) - (before_w.get(ticker) or 0.0)
        else:
            strength = scores.get(ticker)
        state.execute(
            "INSERT OR IGNORE INTO signal_events (as_of, strategy_id, ticker, kind, tick_id,"
            " strength, reason_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                day,
                sid,
                ticker,
                kind,
                tick_id,
                strength,
                json.dumps(reason, sort_keys=True, default=str),
                now,
            ],
        )
        events.append(SignalEvent(sid, ticker, kind, strength, reason))
    return events


_VERBS: dict[str, tuple[str, str]] = {
    "entry": ("bought", "now scores"),
    "exit": ("sold out of", "no longer scores"),
    "increase": ("added to", "scores"),
    "decrease": ("trimmed", "scores"),
    "risk": ("flagged", "flags"),
}


def _reason(
    strategy: Any,
    lake: DuckDBLake,
    sid: str,
    ticker: str,
    kind: str,
    as_of: date,
    *,
    score: float | None,
    rank: int | None,
    of: int,
    before: float | None,
    after: float | None,
    model_book: bool,
) -> dict[str, Any]:
    book_verb, score_verb = _VERBS[kind]
    if model_book:
        # A test book's score is the strategy's own scale, not a return:
        # the text gives its weight and rank only.
        text = f"The test book {book_verb} {ticker}"
        if after is not None and kind != "exit":
            text += f" (now {after:.1%} of the book)"
        if rank is not None:
            text += f", rank {rank} of {of}"
    else:
        text = f"{sid} {score_verb} {ticker}"
        if score is not None and rank is not None:
            text += f": expected return {score:.2%}, rank {rank} of {of}"
    reason: dict[str, Any] = {
        "text": text + ".",
        "score": score,
        "rank": rank,
        "model_weight_before": before,
        "model_weight_after": after,
        "source": "model_book" if model_book else "scores",
    }
    explain = getattr(strategy, "explain", None)
    if callable(explain):
        try:
            extra = explain(ticker, as_of, lake)
            if isinstance(extra, Mapping):
                reason.update({str(k): v for k, v in extra.items()})
        except Exception as exc:
            reason["explain_error"] = f"{type(exc).__name__}: {exc}"
    return reason
