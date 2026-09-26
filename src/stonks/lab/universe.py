"""Point-in-time universes (BL-37, principle P14).

A universe of names that are alive today inflates every cross-sectional
backtest, because the names that died are missing. :func:`resolve` answers
"which tickers were in this universe on this date" for three kinds of
reference:

* a universe id (``str``): the ``universe_membership`` rows of that id;
* a static list of tickers: returned as given (no survivorship protection,
  the lab preflight warns about it);
* a :class:`UniverseRule` (or a mapping of its fields): instruments that
  were listed on the date, with a delisted name included up to its
  ``delisted_date``, filtered by asset class, excluded sectors and average
  daily dollar volume over the bars up to the date.

:func:`resolve_window` is the union over a window, which is what a backtest
must load so names that left or died inside the window are in its data.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from stonks.core.interval import Interval

if TYPE_CHECKING:  # pragma: no cover
    from stonks.store.lake import DuckDBLake


@dataclass(frozen=True)
class UniverseRule:
    """A rule-based universe evaluated on each date.

    ``min_adv`` is the minimum average daily dollar volume (``close *
    volume``) over the last ``adv_window_bars`` daily bars up to the date.
    ``min_price`` is the minimum last daily close on or before the date.
    ``sectors`` and ``exchanges`` keep only instruments in those sets;
    ``exclude_sectors`` drops some. ``None`` fields do not filter."""

    min_adv: float | None = None
    asset_classes: tuple[str, ...] | None = None
    exclude_sectors: tuple[str, ...] = ()
    adv_window_bars: int = 20
    min_price: float | None = None
    sectors: tuple[str, ...] | None = None
    exchanges: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if self.adv_window_bars < 1:
            raise ValueError(f"adv_window_bars must be >= 1, got {self.adv_window_bars}")
        for name in ("asset_classes", "sectors", "exchanges"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, tuple(value))
        object.__setattr__(self, "exclude_sectors", tuple(self.exclude_sectors))

    @classmethod
    def from_mapping(cls, spec: Mapping[str, Any]) -> UniverseRule:
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(spec) - known)
        if unknown:
            raise ValueError(f"unknown universe rule keys {unknown}; expected {sorted(known)}")
        return cls(**dict(spec))


UniverseRef = str | Sequence[str] | UniverseRule | Mapping[str, Any]


def is_point_in_time(ref: UniverseRef) -> bool:
    """False for a static ticker list, which carries survivorship bias."""
    return isinstance(ref, str | UniverseRule | Mapping)


def resolve(lake: DuckDBLake, ref: UniverseRef, as_of: date) -> list[str]:
    """Tickers in ``ref`` on ``as_of`` (see the module doc)."""
    if isinstance(ref, str):
        _require_known(lake, ref)
        return lake.members_as_of(ref, as_of)
    if isinstance(ref, UniverseRule | Mapping):
        return _resolve_rule(lake, _as_rule(ref), as_of)
    return list(dict.fromkeys(ref))


def resolve_window(lake: DuckDBLake, ref: UniverseRef, start: date, end: date) -> list[str]:
    """Tickers in ``ref`` on at least one day of ``[start, end]``. A rule
    is sampled on ``start``, the first of every month and ``end``."""
    if start > end:
        raise ValueError(f"start {start} is after end {end}")
    if isinstance(ref, str):
        _require_known(lake, ref)
        return lake.members_between(ref, start, end)
    if isinstance(ref, UniverseRule | Mapping):
        rule = _as_rule(ref)
        found: set[str] = set()
        for day in _sample_dates(start, end):
            found.update(_resolve_rule(lake, rule, day))
        return sorted(found)
    return list(dict.fromkeys(ref))


def _require_known(lake: DuckDBLake, universe_id: str) -> None:
    if universe_id not in lake.universe_ids():
        raise KeyError(
            f"unknown universe {universe_id!r}: no universe_membership rows "
            "(load them with DuckDBLake.upsert_universe_membership)"
        )


def _as_rule(ref: UniverseRule | Mapping[str, Any]) -> UniverseRule:
    return ref if isinstance(ref, UniverseRule) else UniverseRule.from_mapping(ref)


def _sample_dates(start: date, end: date) -> list[date]:
    out = [start]
    month = date(start.year, start.month, 1)
    while True:
        month = date(month.year + month.month // 12, month.month % 12 + 1, 1)
        if month > end:
            break
        out.append(month)
    if end != start:
        out.append(end)
    return out


def _resolve_rule(lake: DuckDBLake, rule: UniverseRule, as_of: date) -> list[str]:
    day_after = datetime.combine(as_of + timedelta(days=1), datetime.min.time())
    where = [
        "(i.ipo_date IS NULL OR i.ipo_date <= ?)",
        "(i.delisted_date IS NULL OR i.delisted_date > ?)",
        # delisted with no known date: alive while the lake has bars on or
        # after the date
        """NOT (COALESCE(i.is_delisted, FALSE) AND i.delisted_date IS NULL
               AND NOT EXISTS (SELECT 1 FROM bars b WHERE b.ticker = i.id
                                  AND b.interval = ? AND b.timestamp >= ?))""",
    ]
    params: list[Any] = [
        as_of,
        as_of,
        str(Interval.DAY_1),
        datetime.combine(as_of, datetime.min.time()),
    ]
    if rule.asset_classes is not None:
        where.append("i.asset_class = ANY(?)")
        params.append(list(rule.asset_classes))
    if rule.exclude_sectors:
        where.append("NOT (COALESCE(i.sector, '') = ANY(?))")
        params.append(list(rule.exclude_sectors))
    if rule.sectors is not None:
        where.append("COALESCE(i.sector, '') = ANY(?)")
        params.append(list(rule.sectors))
    if rule.exchanges is not None:
        # the instrument's venue (NYSE) or the ticker's exchange suffix (US)
        where.append("(COALESCE(i.exchange, '') = ANY(?) OR string_split(i.id, '.')[-1] = ANY(?))")
        params += [list(rule.exchanges), list(rule.exchanges)]
    if rule.min_price is not None:
        where.append(
            """(SELECT b.close FROM bars b WHERE b.ticker = i.id AND b.interval = ?
                  AND b.timestamp < ? ORDER BY b.timestamp DESC LIMIT 1) >= ?"""
        )
        params += [str(Interval.DAY_1), day_after, rule.min_price]
    sql = f"SELECT i.id AS ticker FROM instruments i WHERE {' AND '.join(where)}"
    if rule.min_adv is not None:
        sql = f"""
            WITH candidates AS ({sql}),
            recent AS (
                SELECT b.ticker, b.close * b.volume AS dollar_volume,
                       row_number() OVER (PARTITION BY b.ticker ORDER BY b.timestamp DESC) AS rn
                  FROM bars b JOIN candidates c ON c.ticker = b.ticker
                 WHERE b.interval = ? AND b.timestamp < ?
            )
            SELECT ticker FROM recent WHERE rn <= ?
             GROUP BY ticker HAVING AVG(dollar_volume) >= ?
        """
        params += [str(Interval.DAY_1), day_after, rule.adv_window_bars, rule.min_adv]
    rows = lake.con.execute(f"SELECT ticker FROM ({sql}) ORDER BY ticker", params).fetchall()
    return [r[0] for r in rows]
