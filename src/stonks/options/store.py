"""Lake persistence for option contracts and end-of-day quotes (migration
017). Reads return domain values (:class:`OptionContract`,
:class:`OptionQuote`), never frames of vendor columns."""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from datetime import date
from typing import TYPE_CHECKING, Any

import pandas as pd

from stonks.core.options import OptionContract
from stonks.ingest.option_schemas import OptionQuoteRow
from stonks.options.chain import ChainSnapshot, OptionQuote

if TYPE_CHECKING:  # pragma: no cover
    from stonks.store.lake import DuckDBLake

_QUOTE_COLS = (
    "contract_id",
    "as_of",
    "source",
    "underlying",
    "bid",
    "ask",
    '"last"',
    "volume",
    "open_interest",
    "underlying_price",
    "vendor_iv",
    "vendor_delta",
    "vendor_gamma",
    "vendor_theta",
    "vendor_vega",
    "vendor_rho",
)


def _opt(value: Any) -> float | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return None
    return float(value)


def _day(value: Any) -> date:
    if isinstance(value, date):
        return value if type(value) is date else date(value.year, value.month, value.day)
    return date.fromisoformat(str(value)[:10])


class OptionStore:
    """Reads and writes on an open lake."""

    def __init__(self, lake: DuckDBLake) -> None:
        self._lake = lake

    # ---- writes -------------------------------------------------------------

    def upsert_quotes(self, rows: Iterable[OptionQuoteRow], source: str) -> int:
        """Write the rows' contracts and quotes. Idempotent: a re-run
        replaces the quotes of the same ``(contract, day, source)`` and
        widens each contract's ``first_seen`` / ``last_seen``."""
        rows = list(rows)
        if not rows:
            return 0
        contracts: dict[str, dict[str, Any]] = {}
        quotes: dict[tuple[str, date], dict[str, Any]] = {}
        for r in rows:
            cid = r.contract_id
            seen = contracts.get(cid)
            if seen is None:
                contracts[cid] = {
                    "contract_id": cid,
                    "underlying": r.underlying,
                    "expiry": r.expiry,
                    "strike": r.strike,
                    "right": r.right,
                    "style": r.style,
                    "multiplier": r.multiplier,
                    "settlement": r.settlement,
                    "currency": r.currency,
                    "exchange": r.exchange,
                    "occ_symbol": r.occ_symbol,
                    "first_seen": r.as_of,
                    "last_seen": r.as_of,
                }
            else:
                seen["first_seen"] = min(seen["first_seen"], r.as_of)
                seen["last_seen"] = max(seen["last_seen"], r.as_of)
            quotes[(cid, r.as_of)] = {
                "contract_id": cid,
                "as_of": r.as_of,
                "source": source,
                "underlying": r.underlying,
                "bid": r.bid,
                "ask": r.ask,
                "last": r.last,
                "volume": r.volume,
                "open_interest": r.open_interest,
                "underlying_price": r.underlying_price,
                "vendor_iv": r.iv,
                "vendor_delta": r.delta,
                "vendor_gamma": r.gamma,
                "vendor_theta": r.theta,
                "vendor_vega": r.vega,
                "vendor_rho": r.rho,
            }
        con = self._lake.con
        with self._lake.transaction():
            name = f"_opt_contracts_{uuid.uuid4().hex}"
            con.register(name, pd.DataFrame(list(contracts.values())))
            try:
                con.execute(
                    f"""
                    INSERT INTO option_contracts
                        (contract_id, underlying, expiry, strike, "right", style, multiplier,
                         settlement, currency, exchange, occ_symbol, first_seen, last_seen)
                    SELECT contract_id, underlying, expiry, strike, "right", style, multiplier,
                           settlement, currency, exchange, occ_symbol, first_seen, last_seen
                    FROM {name}
                    ON CONFLICT (contract_id) DO UPDATE SET
                        occ_symbol = COALESCE(EXCLUDED.occ_symbol, option_contracts.occ_symbol),
                        exchange = COALESCE(EXCLUDED.exchange, option_contracts.exchange),
                        first_seen = LEAST(option_contracts.first_seen, EXCLUDED.first_seen),
                        last_seen = GREATEST(option_contracts.last_seen, EXCLUDED.last_seen)
                    """
                )
            finally:
                con.unregister(name)
            name = f"_opt_quotes_{uuid.uuid4().hex}"
            frame = pd.DataFrame(list(quotes.values()))
            con.register(name, frame)
            try:
                cols = ", ".join(_QUOTE_COLS)
                updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in _QUOTE_COLS[3:])
                con.execute(
                    f"INSERT INTO option_quotes ({cols}) SELECT {cols} FROM {name} "
                    f"ON CONFLICT (contract_id, as_of, source) DO UPDATE SET {updates}"
                )
            finally:
                con.unregister(name)
        return len(quotes)

    # ---- reads --------------------------------------------------------------

    def contracts(self, contract_ids: Sequence[str] | None = None) -> dict[str, OptionContract]:
        sql = (
            'SELECT contract_id, underlying, expiry, strike, "right", style, multiplier, '
            "settlement, currency FROM option_contracts"
        )
        params: list[Any] = []
        if contract_ids is not None:
            if not contract_ids:
                return {}
            sql += " WHERE contract_id IN (SELECT UNNEST(?))"
            params.append(list(contract_ids))
        out: dict[str, OptionContract] = {}
        for cid, und, exp, strike, right, style, mult, settle, ccy in self._lake.con.execute(
            sql, params
        ).fetchall():
            out[cid] = OptionContract(
                underlying=und,
                expiry=_day(exp),
                strike=float(strike),
                right=right,
                multiplier=float(mult),
                style=style,
                settlement=settle,
                currency=ccy or "USD",
            )
        return out

    def quotes(
        self,
        underlying: str,
        start: date,
        end: date,
        *,
        source: str | None = None,
    ) -> list[OptionQuote]:
        """Every quote of ``underlying`` dated in ``[start, end]``. With
        several sources for one contract and day, the first source by name
        wins unless ``source`` picks one."""
        sql = f"""
            SELECT q.contract_id, q.as_of, q.bid, q.ask, q."last", q.volume, q.open_interest,
                   q.underlying_price, q.vendor_iv, q.vendor_delta, q.vendor_gamma,
                   q.vendor_theta, q.vendor_vega, q.vendor_rho,
                   c.underlying, c.expiry, c.strike, c."right", c.style, c.multiplier,
                   c.settlement, c.currency
            FROM option_quotes q JOIN option_contracts c USING (contract_id)
            WHERE q.underlying = ? AND q.as_of BETWEEN ? AND ?
            {"AND q.source = ?" if source else ""}
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY q.contract_id, q.as_of ORDER BY q.source) = 1
            ORDER BY q.as_of, c.expiry, c."right", c.strike
        """
        params: list[Any] = [underlying, start, end]
        if source:
            params.append(source)
        out: list[OptionQuote] = []
        for row in self._lake.con.execute(sql, params).fetchall():
            contract = OptionContract(
                underlying=row[14],
                expiry=_day(row[15]),
                strike=float(row[16]),
                right=row[17],
                multiplier=float(row[19]),
                style=row[18],
                settlement=row[20],
                currency=row[21] or "USD",
            )
            out.append(
                OptionQuote(
                    contract=contract,
                    as_of=_day(row[1]),
                    bid=_opt(row[2]),
                    ask=_opt(row[3]),
                    last=_opt(row[4]),
                    volume=_opt(row[5]),
                    open_interest=_opt(row[6]),
                    underlying_price=_opt(row[7]),
                    iv=_opt(row[8]),
                    delta=_opt(row[9]),
                    gamma=_opt(row[10]),
                    theta=_opt(row[11]),
                    vega=_opt(row[12]),
                    rho=_opt(row[13]),
                )
            )
        return out

    def chain(self, underlying: str, as_of: date, *, source: str | None = None) -> ChainSnapshot:
        """The snapshot of ``underlying`` on ``as_of`` (empty if none)."""
        quotes = self.quotes(underlying, as_of, as_of, source=source)
        spots = [q.underlying_price for q in quotes if q.underlying_price]
        return ChainSnapshot(underlying, as_of, tuple(quotes), spot=spots[0] if spots else None)

    def quote_days(self, underlying: str) -> list[date]:
        rows = self._lake.con.execute(
            "SELECT DISTINCT as_of FROM option_quotes WHERE underlying = ? ORDER BY as_of",
            [underlying],
        ).fetchall()
        return [_day(r[0]) for r in rows]
