"""Fund snapshots read point in time from the lake (roadmap 23.14)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

import pandas as pd

from stonks.logging import get_logger

_log = get_logger("stonks.funds")


@dataclass(frozen=True)
class Constituent:
    """One holding of a fund. ``weight`` is its share of the fund (0.07 = 7 %)."""

    holding: str
    weight: float
    name: str | None = None
    sector: str | None = None
    country: str | None = None


@dataclass(frozen=True)
class FundSnapshot:
    """A fund's holdings as published on ``as_of``, largest first."""

    fund: str
    as_of: date
    source: str
    constituents: tuple[Constituent, ...]

    @property
    def covered(self) -> float:
        """Share of the fund the listed holdings explain (at most 1)."""
        return min(sum(c.weight for c in self.constituents), 1.0)

    def sector_weights(self) -> dict[str, float]:
        """Each known sector's share of the fund. Holdings without a sector
        and the unlisted rest are left out."""
        out: dict[str, float] = {}
        for c in self.constituents:
            if c.sector:
                out[c.sector] = out.get(c.sector, 0.0) + c.weight
        return out


def load_fund_snapshots(lake: Any, tickers: Iterable[str], as_of: date) -> dict[str, FundSnapshot]:
    """The snapshot known on ``as_of`` of each of ``tickers`` that is a fund
    with stored holdings. A holding's sector and country come from
    ``instruments`` when Stonks knows the name (the same labels as a direct
    holding), else from the fund's own list."""
    names = sorted({str(t) for t in tickers if t})
    if not names:
        return {}
    df = lake.fund_holdings(names, as_of=as_of)
    if df.empty:
        return {}
    profiles = _profiles(lake, sorted(set(df["holding"])))
    grouped: dict[str, list[Constituent]] = {}
    meta: dict[str, tuple[date, str]] = {}
    for row in df.to_dict("records"):
        fund = str(row["fund"])
        sector, country = profiles.get(str(row["holding"]), (None, None))
        grouped.setdefault(fund, []).append(
            Constituent(
                holding=str(row["holding"]),
                weight=float(row["weight"]),
                name=_text(row.get("name")),
                sector=sector or _text(row.get("sector")),
                country=country or _text(row.get("country")),
            )
        )
        meta[fund] = (row["as_of"], str(row["source"]))
    return {
        fund: FundSnapshot(fund, meta[fund][0], meta[fund][1], tuple(rows))
        for fund, rows in grouped.items()
    }


def safe_fund_snapshots(
    lake: Any, tickers: Iterable[str], as_of: date
) -> Mapping[str, FundSnapshot]:
    """:func:`load_fund_snapshots`, or an empty map (logged) when the read
    fails, so a missing table never stops a tick."""
    try:
        return load_fund_snapshots(lake, tickers, as_of)
    except Exception as exc:  # the rule falls back to the plain sector cap
        _log.warning("funds.snapshots_failed", error=str(exc))
        return {}


def _profiles(lake: Any, tickers: list[str]) -> dict[str, tuple[str | None, str | None]]:
    if not tickers:
        return {}
    df = lake.sql("SELECT id, sector, country_iso FROM instruments WHERE id = ANY(?)", [tickers])
    return {
        str(r["id"]): (_text(r["sector"]), (_text(r["country_iso"]) or "").upper() or None)
        for r in df.to_dict("records")
    }


def _text(value: Any) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    return text or None
