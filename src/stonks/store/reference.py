"""Lake persistence for reference data (migration 026): exchange details
and holidays, US Treasury rates, the IPO and split calendars, and the
identifiers on ``instruments``.

Revisable tables keep a ``<table>_versions`` twin (principle P12): each
upsert writes the latest view and appends the new row to the twin unless
it equals the newest version already there, stamped ``known_at`` (now in
naive UTC by default). Point-in-time reads take ``known_at`` and pick the
newest version seen by then.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

import pandas as pd

from stonks.frames import columns
from stonks.ingest.reference_schemas import (
    ExchangeDetailsRow,
    InstrumentIdentifiersRow,
    IpoEventRow,
    SplitEventRow,
    TreasuryRateRow,
)

if TYPE_CHECKING:  # pragma: no cover
    from stonks.store.lake import DuckDBLake

_EXCHANGE_COLS = (
    "exchange",
    "source",
    "name",
    "operating_mics",
    "country",
    "currency",
    "timezone",
    "open_local",
    "close_local",
    "open_utc",
    "close_utc",
    "working_days",
)
_EXCHANGE_PK = ("exchange", "source")
_HOLIDAY_COLS = ("exchange", "holiday_date", "source", "name", "kind", "early_close", "known_at")
_TREASURY_COLS = (
    "curve",
    "tenor",
    "measure",
    "observation_date",
    "source",
    "rate",
    "maturity_date",
    "cusip",
)
_TREASURY_PK = ("curve", "tenor", "measure", "observation_date", "source")
_IPO_COLS = (
    "ticker",
    "filing_date",
    "source",
    "exchange",
    "name",
    "currency",
    "amended_date",
    "listing_date",
    "price_low",
    "price_high",
    "offer_price",
    "shares",
    "status",
)
_IPO_PK = ("ticker", "filing_date", "source")
_SPLIT_COLS = ("ticker", "split_date", "source", "old_shares", "new_shares", "optionable")
_SPLIT_PK = ("ticker", "split_date", "source")
_IDENTIFIER_COLS = ("isin", "cusip", "cik", "open_figi", "lei")


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _time_text(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _exchange_of(ticker: str) -> str | None:
    base, dot, suffix = ticker.rpartition(".")
    return suffix.upper() if dot and base else None


class ReferenceStore:
    """Reads and writes reference data on an open lake."""

    def __init__(self, lake: DuckDBLake) -> None:
        self._lake = lake

    # ---- the versioned upsert ------------------------------------------------------

    def _upsert_versioned(
        self,
        df: pd.DataFrame,
        *,
        table: str,
        cols: tuple[str, ...],
        pk: tuple[str, ...],
        known_at: datetime,
    ) -> int:
        """Upsert ``df`` into ``table`` and append each upserted row to
        ``<table>_versions`` unless it equals the newest stored version."""
        if df.empty:
            return 0
        lake = self._lake
        batch = columns(df, cols).drop_duplicates(subset=list(pk), keep="last")
        key_match = " AND ".join(f"k.{c} = t.{c}" for c in pk)
        same_key = " AND ".join(f"l.{c} = t.{c}" for c in pk)
        unchanged = " AND ".join(f"l.{c} IS NOT DISTINCT FROM t.{c}" for c in cols)
        partition = ", ".join(f"v.{c}" for c in pk)
        with lake.transaction():
            count = lake._upsert(batch, table=table, cols=cols, pk=pk)
            with lake._registered(pd.DataFrame(batch[list(pk)]), "_keys"):
                lake.con.execute(
                    f"""
                    INSERT INTO {table}_versions ({", ".join(cols)}, known_at)
                    SELECT {", ".join(f"t.{c}" for c in cols)}, CAST(? AS TIMESTAMP)
                      FROM {table} t
                     WHERE EXISTS (SELECT 1 FROM _keys k WHERE {key_match})
                       AND NOT EXISTS (
                            SELECT 1 FROM (
                                SELECT v.*, row_number() OVER (
                                           PARTITION BY {partition} ORDER BY v.known_at DESC
                                       ) AS rn
                                  FROM {table}_versions v
                                 WHERE EXISTS (
                                     SELECT 1 FROM _keys k
                                      WHERE {" AND ".join(f"k.{c} = v.{c}" for c in pk)})
                            ) l
                             WHERE l.rn = 1 AND {same_key} AND {unchanged})
                    ON CONFLICT DO NOTHING
                    """,
                    [known_at],
                )
        return count

    def _versions_as_of(
        self, table: str, pk: tuple[str, ...], where: str, params: list[Any], known_at: datetime
    ) -> pd.DataFrame:
        """The newest version of each key matching ``where`` seen by ``known_at``."""
        partition = ", ".join(pk)
        return self._lake.con.execute(
            f"""
            SELECT * EXCLUDE (rn) FROM (
                SELECT *, row_number() OVER (PARTITION BY {partition} ORDER BY known_at DESC) AS rn
                  FROM {table}_versions
                 WHERE known_at <= CAST(? AS TIMESTAMP) AND {where}
            ) WHERE rn = 1
            """,
            [known_at, *params],
        ).df()

    # ---- exchanges -----------------------------------------------------------------

    def upsert_exchange_details(
        self, rows: Iterable[ExchangeDetailsRow], *, source: str, known_at: datetime | None = None
    ) -> int:
        """Hours and zone per exchange (versioned) and its holidays. A
        holiday keeps the ``known_at`` it was first seen with."""
        stamp = known_at or _now()
        items = list(rows)
        details = pd.DataFrame(
            [
                {
                    "exchange": r.exchange,
                    "source": source,
                    "name": r.name,
                    "operating_mics": ",".join(r.operating_mics) or None,
                    "country": r.country,
                    "currency": r.currency,
                    "timezone": r.timezone,
                    "open_local": _time_text(r.open_local),
                    "close_local": _time_text(r.close_local),
                    "open_utc": _time_text(r.open_utc),
                    "close_utc": _time_text(r.close_utc),
                    "working_days": ",".join(str(d) for d in r.working_days) or None,
                }
                for r in items
            ],
            columns=list(_EXCHANGE_COLS),
        )
        holidays = pd.DataFrame(
            [
                {
                    "exchange": r.exchange,
                    "holiday_date": h.holiday_date,
                    "source": source,
                    "name": h.name,
                    "kind": h.kind,
                    "early_close": _time_text(h.early_close),
                    "known_at": stamp,
                }
                for r in items
                for h in r.holidays
            ],
            columns=list(_HOLIDAY_COLS),
        )
        lake = self._lake
        with lake.transaction():
            count = self._upsert_versioned(
                details, table="exchange_details", cols=_EXCHANGE_COLS, pk=_EXCHANGE_PK,
                known_at=stamp,
            )  # fmt: skip
            if not holidays.empty:
                batch = holidays.drop_duplicates(
                    subset=["exchange", "holiday_date", "source"], keep="last"
                )
                with lake._registered(batch, "_in"):
                    lake.con.execute(
                        f"""
                        INSERT INTO exchange_holidays ({", ".join(_HOLIDAY_COLS)})
                        SELECT {", ".join(_HOLIDAY_COLS)} FROM _in
                        ON CONFLICT (exchange, holiday_date, source) DO UPDATE SET
                            name = EXCLUDED.name, kind = EXCLUDED.kind,
                            early_close = EXCLUDED.early_close
                        """
                    )
        return count + len(holidays)

    def exchange_holidays(
        self, exchange: str, start: date | None = None, end: date | None = None
    ) -> pd.DataFrame:
        where: list[str] = ["exchange = ?"]
        params: list[Any] = [exchange.upper()]
        if start is not None:
            where.append("holiday_date >= ?")
            params.append(start)
        if end is not None:
            where.append("holiday_date <= ?")
            params.append(end)
        return self._lake.con.execute(
            f"SELECT * FROM exchange_holidays WHERE {' AND '.join(where)} ORDER BY holiday_date",
            params,
        ).df()

    # ---- Treasury ------------------------------------------------------------------

    def upsert_treasury_rates(
        self, rows: Iterable[TreasuryRateRow], *, source: str, known_at: datetime | None = None
    ) -> int:
        df = pd.DataFrame(
            [{**r.model_dump(), "source": source} for r in rows], columns=list(_TREASURY_COLS)
        )
        return self._upsert_versioned(
            df,
            table="treasury_rates",
            cols=_TREASURY_COLS,
            pk=_TREASURY_PK,
            known_at=known_at or _now(),
        )

    def treasury_curve(
        self,
        day: date,
        curve: str,
        *,
        measure: str = "yield",
        known_at: datetime | None = None,
    ) -> dict[str, float | None]:
        """``tenor -> rate`` of ``curve`` on ``day``. With ``known_at``,
        the versions seen by then (P12); without, the latest view."""
        where = "observation_date = ? AND curve = ? AND measure = ?"
        params: list[Any] = [day, curve, measure]
        if known_at is None:
            rows = self._lake.con.execute(
                f"SELECT tenor, rate FROM treasury_rates WHERE {where}", params
            ).fetchall()
        else:
            frame = self._versions_as_of("treasury_rates", _TREASURY_PK, where, params, known_at)
            rows = list(zip(frame["tenor"], frame["rate"], strict=True))
        return {str(t): (None if r is None or pd.isna(r) else float(r)) for t, r in rows}

    # ---- IPO and split calendars ---------------------------------------------------

    def upsert_ipos(
        self, rows: Iterable[IpoEventRow], *, source: str, known_at: datetime | None = None
    ) -> int:
        """Per ``(ticker, filing_date)``; an offering with no filing date is
        keyed by its listing date, and one with neither is skipped."""
        records = []
        for r in rows:
            key_day = r.filing_date or r.listing_date or r.amended_date
            if key_day is None:
                continue
            record = r.model_dump()
            record.update(filing_date=key_day, source=source, exchange=_exchange_of(r.ticker))
            records.append(record)
        df = pd.DataFrame(records, columns=list(_IPO_COLS))
        return self._upsert_versioned(
            df, table="ipo_calendar", cols=_IPO_COLS, pk=_IPO_PK, known_at=known_at or _now()
        )

    def upsert_splits(
        self, rows: Iterable[SplitEventRow], *, source: str, known_at: datetime | None = None
    ) -> int:
        df = pd.DataFrame(
            [{**r.model_dump(), "source": source} for r in rows], columns=list(_SPLIT_COLS)
        )
        return self._upsert_versioned(
            df, table="split_calendar", cols=_SPLIT_COLS, pk=_SPLIT_PK, known_at=known_at or _now()
        )

    # ---- identifiers ---------------------------------------------------------------

    def update_identifiers(self, rows: Sequence[InstrumentIdentifiersRow]) -> int:
        """Set the identifiers of instruments the lake already has. A NULL
        never clears a stored identifier. Returns the instruments touched."""
        if not rows:
            return 0
        df = pd.DataFrame(
            [r.model_dump() for r in rows], columns=["ticker", *_IDENTIFIER_COLS]
        ).drop_duplicates(subset=["ticker"], keep="last")
        lake = self._lake
        sets = ", ".join(f"{c} = COALESCE(i.{c}, instruments.{c})" for c in _IDENTIFIER_COLS)
        with lake.transaction(), lake._registered(df, "_ids"):
            touched = lake.con.execute(
                "SELECT COUNT(*) FROM instruments WHERE id IN (SELECT ticker FROM _ids)"
            ).fetchone()
            lake.con.execute(
                f"UPDATE instruments SET {sets} FROM _ids i WHERE instruments.id = i.ticker"
            )
        return int(touched[0]) if touched else 0
