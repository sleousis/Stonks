"""FX pair parsing for ``stonks ingest fx`` (roadmap 20.5).

The pipeline's ``run_fx_rates`` does the fetching and the ``ingest_runs``
row. This module only turns what a person types into ``(base, quote)``
pairs of ISO 4217 codes."""

from __future__ import annotations

import re

_PAIR = re.compile(r"^([A-Za-z]{3})[/\-_ ]?([A-Za-z]{3})$")


def parse_pair(text: str) -> tuple[str, str]:
    """``EURUSD``, ``EUR/USD`` or ``eur-usd`` -> ``("EUR", "USD")``."""
    match = _PAIR.match(text.strip())
    if match is None:
        raise ValueError(f"not a currency pair: {text!r} (expected e.g. EURUSD or EUR/USD)")
    base, quote = match.group(1).upper(), match.group(2).upper()
    if base == quote:
        raise ValueError(f"a pair needs two different currencies: {text!r}")
    return base, quote


def parse_pairs(text: str) -> list[tuple[str, str]]:
    """Comma-separated pairs, without repeats, in the order given."""
    out: list[tuple[str, str]] = []
    for part in text.split(","):
        if part.strip():
            pair = parse_pair(part)
            if pair not in out:
                out.append(pair)
    return out
