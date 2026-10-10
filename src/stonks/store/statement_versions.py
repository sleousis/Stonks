"""Which version of a statement row a decision may read (P12, DuckDB 018).

Each statement table keeps a ``<table>_versions`` twin: every version of a
row with ``known_at``, the time Stonks first saw it. :func:`known_versions`
picks, per ``(ticker, period_end, frequency)``, the latest version known at
a decision:

- the first version of a period is the one as filed: it counts whenever
  it was seen (the caller's filing filter still applies), so history
  ingested late stays usable;
- a later version (a restatement) counts only once ``known_at`` is at or
  before the decision.

The macro versions of DuckDB 025 use the same rule with their own key.

Pure pandas, shared by the lake's own ``get_statements_as_of`` and the
point-in-time view.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

__all__ = ["KNOWN_AT", "MACRO_KEY", "STATEMENT_KEY", "known_versions"]

STATEMENT_KEY: tuple[str, ...] = ("ticker", "period_end", "frequency")
KNOWN_AT = "known_at"
#: One macro observation's identity within a series (DuckDB 025).
MACRO_KEY: tuple[str, ...] = ("observation_date",)


def known_versions(
    versions: pd.DataFrame,
    known_by: datetime,
    filed: pd.Series | None = None,
    *,
    key: tuple[str, ...] = STATEMENT_KEY,
) -> pd.DataFrame:
    """Per period, the latest version known by ``known_by`` among the rows
    ``filed`` allows (a boolean mask aligned by position; ``None``: all).
    Rows keep their input order and ``known_at`` is dropped. ``key`` names
    the columns of one row's identity (a statement period by default, an
    observation for macro prints, DuckDB 025)."""
    if versions.empty:
        return versions.drop(columns=[KNOWN_AT], errors="ignore").reset_index(drop=True)
    frame = versions.reset_index(drop=True)
    stamps = pd.Series(pd.to_datetime(frame[KNOWN_AT]).to_numpy(), index=frame.index)
    keys = [frame[c] for c in key]
    first = stamps == stamps.groupby(keys).transform("min")
    eligible = first | (stamps <= pd.Timestamp(known_by))
    if filed is not None:
        eligible &= pd.Series(filed.to_numpy(dtype=bool), index=frame.index)
    mask = eligible.to_numpy()
    chosen = frame.loc[mask]
    order = stamps.loc[mask].sort_values(kind="stable")
    latest = chosen.loc[order.index].groupby(list(key), sort=False).tail(1)
    out = frame.loc[sorted(latest.index)].drop(columns=[KNOWN_AT])
    return out.reset_index(drop=True)
