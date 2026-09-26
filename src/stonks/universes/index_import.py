"""Import an index constituent history from CSV or JSON text.

CSV has a header ``date,ticker,action``. ``action`` is ``add`` or
``remove`` for a change, or ``member`` for a snapshot row (the members on
that date; every ``member`` row must share one date)::

    date,ticker,action
    2024-01-02,AAPL.US,member
    2023-06-01,NEWCO.US,add
    2023-06-01,OLDCO.US,remove

JSON has the same content as one object::

    {"as_of": "2024-01-02", "constituents": ["AAPL.US"],
     "changes": [{"date": "2023-06-01", "ticker": "OLDCO.US", "action": "remove"}]}

Tickers are stored as given, so use canonical ids (``AAPL.US``).
"""

from __future__ import annotations

import csv
import io
import json
from datetime import date
from typing import Literal

from stonks.universes.base import IndexChange, IndexHistory

ImportFormat = Literal["csv", "json"]

_ACTIONS = ("add", "remove")


def parse_index_history(
    content: str, fmt: ImportFormat, *, index_id: str, source: str = "import"
) -> IndexHistory:
    """An :class:`IndexHistory` from ``content`` (``ValueError`` when it
    is malformed)."""
    if fmt == "csv":
        as_of, members, changes = _parse_csv(content)
    elif fmt == "json":
        as_of, members, changes = _parse_json(content)
    else:
        raise ValueError(f"unknown format {fmt!r}; use csv or json")
    if not members and not changes:
        raise ValueError("the history has no members and no changes")
    return IndexHistory(
        index_id=index_id,
        as_of=as_of,
        constituents=tuple(dict.fromkeys(members)),
        changes=tuple(dict.fromkeys(changes)),
        source=source,
    )


def _date(value: object, where: str) -> date:
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        raise ValueError(f"{where}: {value!r} is not a YYYY-MM-DD date") from None


def _change(day: date, ticker: str, action: str, where: str) -> IndexChange:
    action = action.strip().lower()
    if action not in _ACTIONS:
        raise ValueError(f"{where}: action {action!r} must be add or remove")
    if not ticker.strip():
        raise ValueError(f"{where}: empty ticker")
    return IndexChange(ticker.strip(), day, action)  # type: ignore[arg-type]


def _parse_csv(content: str) -> tuple[date | None, list[str], list[IndexChange]]:
    reader = csv.DictReader(io.StringIO(content.strip()))
    header = [h.strip().lower() for h in reader.fieldnames or []]
    if not {"date", "ticker", "action"} <= set(header):
        raise ValueError("the CSV needs a header with date, ticker and action")
    reader.fieldnames = header
    as_of: date | None = None
    members: list[str] = []
    changes: list[IndexChange] = []
    for n, row in enumerate(reader, start=2):
        where = f"line {n}"
        day = _date(row.get("date"), where)
        ticker = (row.get("ticker") or "").strip()
        action = (row.get("action") or "").strip().lower()
        if action == "member":
            if as_of is not None and day != as_of:
                raise ValueError(f"{where}: member rows must share one date ({as_of})")
            as_of = day
            if ticker:
                members.append(ticker)
            continue
        changes.append(_change(day, ticker, action, where))
    return as_of, members, changes


def _parse_json(content: str) -> tuple[date | None, list[str], list[IndexChange]]:
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise ValueError("the JSON must be an object with as_of, constituents and changes")
    members = [str(t).strip() for t in data.get("constituents") or [] if str(t).strip()]
    as_of = _date(data["as_of"], "as_of") if data.get("as_of") else None
    if members and as_of is None:
        raise ValueError("constituents need an as_of date")
    changes = []
    for n, item in enumerate(data.get("changes") or []):
        if not isinstance(item, dict):
            raise ValueError(f"changes[{n}] must be an object")
        where = f"changes[{n}]"
        changes.append(
            _change(
                _date(item.get("date"), where),
                str(item.get("ticker") or ""),
                str(item.get("action") or ""),
                where,
            )
        )
    return as_of, members, changes
