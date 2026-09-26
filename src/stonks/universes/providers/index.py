"""``index`` universes: constituents rebuilt from a change history.

The history is a snapshot (the members on one day) plus dated additions
and removals (:class:`~stonks.universes.base.IndexHistory`). Changes on or
before the snapshot day are walked backwards from it: an addition opens a
member's span on its date, a removal means the name was a member until
then. Changes after the snapshot day are applied forwards. Names still in
the set at the start of the history get ``start_date`` (default: the
earliest change date, the first day the history can vouch for).

``source`` names an index adapter (:mod:`stonks.universes.index_sources`)
to fetch a fresh history on every refresh; the default ``stored`` uses
what was imported (``UniverseStore.save_index_history``).
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field

from stonks.universes.base import (
    IndexHistory,
    Materialized,
    MembershipSpan,
    RefreshContext,
    UniverseProvider,
)

STORED = "stored"


class IndexSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Canonical index code, e.g. ``sp500``.
    index_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_]{0,63}$")
    #: ``stored`` or an index source id (e.g. ``wikipedia_sp500``).
    source: str = STORED
    #: First day of membership for names the history never saw join.
    start_date: date | None = None


class IndexProvider(UniverseProvider):
    kind = "index"
    spec_model = IndexSpec

    def materialize(self, spec: IndexSpec, ctx: RefreshContext) -> Materialized:
        from stonks.universes.store import UniverseStore

        fetched: IndexHistory | None = None
        if spec.source != STORED:
            if ctx.index_source is None:
                raise ValueError(f"index source {spec.source!r} needs an index source factory")
            fetched = ctx.index_source(spec.source).fetch(spec.index_id)
            history = fetched
        else:
            history = UniverseStore(ctx.lake).index_history(spec.index_id)
        if history is None or (not history.constituents and not history.changes):
            raise ValueError(
                f"no constituent history for index {spec.index_id!r}: import one "
                "(CSV or JSON) or pick an index source"
            )
        spans, warnings = spans_from_index_history(history, spec.start_date)
        return Materialized(spans=spans, warnings=warnings, index_history=fetched)


def spans_from_index_history(
    history: IndexHistory, start_date: date | None = None
) -> tuple[list[MembershipSpan], list[str]]:
    """Membership spans and warnings (inconsistent changes are skipped
    with a warning, never guessed). See the module doc."""
    warnings: list[str] = []
    change_dates = [c.change_date for c in history.changes]
    snapshot = history.as_of
    if snapshot is None:
        # no snapshot: the history ends with the last change, walk back from there
        snapshot = max(change_dates) if change_dates else date.today()
    begin = start_date or (min(change_dates) if change_dates else snapshot)
    before = sorted(
        (c for c in history.changes if c.change_date <= snapshot),
        key=lambda c: (c.change_date, c.action == "add"),
        reverse=True,
    )
    after = sorted(
        (c for c in history.changes if c.change_date > snapshot),
        key=lambda c: (c.change_date, c.action == "add"),
    )
    spans: list[MembershipSpan] = []

    def emit(ticker: str, start: date, end: date | None) -> None:
        clamped = start < begin
        start = max(start, begin)
        if end is not None and end <= start:
            if not clamped:  # a span that ends before the history starts is just out of range
                warnings.append(f"{ticker}: span {start}..{end} is empty, skipped")
            return
        spans.append(MembershipSpan(ticker, start, end))

    # forwards from the snapshot: each current member's end, plus new members
    open_end: dict[str, date | None] = dict.fromkeys(dict.fromkeys(history.constituents))
    forward_start: dict[str, date] = {}
    current = set(open_end)
    for change in after:
        t = change.ticker
        if change.action == "remove":
            if t not in current:
                warnings.append(f"{t}: removed on {change.change_date} but not a member, skipped")
                continue
            current.discard(t)
            if t in forward_start:
                emit(t, forward_start.pop(t), change.change_date)
            else:
                open_end[t] = change.change_date
        else:
            if t in current:
                warnings.append(f"{t}: added on {change.change_date} but already a member, skipped")
                continue
            current.add(t)
            forward_start[t] = change.change_date
    for t, start in forward_start.items():
        emit(t, start, None)

    # backwards from the snapshot
    members: dict[str, date | None] = dict(open_end)
    for change in before:
        t = change.ticker
        if change.action == "add":
            if t not in members:
                warnings.append(
                    f"{t}: added on {change.change_date} but not a later member and never "
                    "removed, skipped"
                )
                continue
            emit(t, change.change_date, members.pop(t))
        else:
            if t in members:
                warnings.append(
                    f"{t}: removed on {change.change_date} but still counted as a member, skipped"
                )
                continue
            members[t] = change.change_date
    for t, end in members.items():
        if end is None or end > begin:  # else it left before the history starts
            emit(t, begin, end)
    return spans, warnings
