"""Option contracts as vendor-agnostic values (roadmap 17.1).

An option position is keyed by its canonical contract id, the same way a
stock position is keyed by its ticker:

    <underlying>:<expiry>:<C|P>:<strike>[:<multiplier>]

for example ``AAPL.US:2026-01-16:C:150``. The multiplier part appears only
when it is not the standard 100 (an adjusted contract after a split or a
merger), so ``AAPL.US:2026-01-16:C:100:150`` delivers 150 shares.

Vendor symbols map to it at parse time. The OCC symbol (``AAPL
260116C00150000``: a six-character root padded with spaces, ``YYMMDD``, the
right and the strike times 1000 on eight digits) is one such symbol; it
carries no multiplier and no exchange suffix, so parsing it needs the
underlying ticker.

Style and settlement are not part of the id: a contract id names one
listed series, and style and settlement are facts about that series that
travel with :class:`OptionContract`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

OptionRight = Literal["call", "put"]
ExerciseStyle = Literal["american", "european"]
Settlement = Literal["physical", "cash"]

#: Shares one standard US equity option delivers.
STANDARD_MULTIPLIER = 100.0
#: An option in the money by at least this much is exercised at expiry
#: (OCC exercise by exception).
AUTO_EXERCISE_THRESHOLD = 0.01

_RIGHT_CODE: dict[str, OptionRight] = {"C": "call", "P": "put"}
_CODE_OF: dict[OptionRight, str] = {"call": "C", "put": "P"}


def format_number(value: float) -> str:
    """The shortest plain decimal for a strike or multiplier: ``150``,
    ``152.5``, ``0.375``. Never scientific notation."""
    if not math.isfinite(value):
        raise ValueError(f"not a finite number: {value}")
    text = format(Decimal(repr(float(value))).normalize(), "f")
    return text[:-2] if text.endswith(".0") else text


@dataclass(frozen=True)
class OptionContract:
    """One listed option series."""

    underlying: str
    expiry: date
    strike: float
    right: OptionRight
    multiplier: float = STANDARD_MULTIPLIER
    style: ExerciseStyle = "american"
    settlement: Settlement = "physical"
    currency: str = "USD"

    def __post_init__(self) -> None:
        if not self.underlying or ":" in self.underlying:
            raise ValueError(f"bad underlying {self.underlying!r}")
        if self.right not in _CODE_OF:
            raise ValueError(f"right must be call or put, got {self.right!r}")
        if not (math.isfinite(self.strike) and self.strike > 0):
            raise ValueError(f"strike must be positive, got {self.strike}")
        if not (math.isfinite(self.multiplier) and self.multiplier > 0):
            raise ValueError(f"multiplier must be positive, got {self.multiplier}")
        if self.style not in ("american", "european"):
            raise ValueError(f"style must be american or european, got {self.style!r}")
        if self.settlement not in ("physical", "cash"):
            raise ValueError(f"settlement must be physical or cash, got {self.settlement!r}")
        if isinstance(self.expiry, datetime) or not isinstance(self.expiry, date):
            raise ValueError(f"expiry must be a date, got {self.expiry!r}")

    @property
    def contract_id(self) -> str:
        base = (
            f"{self.underlying}:{self.expiry.isoformat()}:"
            f"{_CODE_OF[self.right]}:{format_number(self.strike)}"
        )
        if self.multiplier == STANDARD_MULTIPLIER:
            return base
        return f"{base}:{format_number(self.multiplier)}"

    @property
    def is_call(self) -> bool:
        return self.right == "call"

    def intrinsic(self, spot: float) -> float:
        """Intrinsic value per share at underlying price ``spot``."""
        if self.is_call:
            return max(spot - self.strike, 0.0)
        return max(self.strike - spot, 0.0)

    def auto_exercises(self, spot: float) -> bool:
        """True when OCC exercise by exception would exercise it at expiry."""
        return self.intrinsic(spot) >= AUTO_EXERCISE_THRESHOLD - 1e-12

    def days_to_expiry(self, as_of: date) -> int:
        return (self.expiry - as_of).days

    def year_fraction(self, as_of: date) -> float:
        """Calendar time to expiry in years (Actual/365)."""
        return max(self.days_to_expiry(as_of), 0) / 365.0

    def occ_symbol(self, root: str | None = None) -> str:
        """The 21-character OCC symbol. ``root`` defaults to the underlying
        without its exchange suffix (``AAPL.US`` -> ``AAPL``)."""
        return occ_symbol(
            root or self.underlying.split(".")[0], self.expiry, self.right, self.strike
        )


def parse_contract_id(
    contract_id: str,
    *,
    style: ExerciseStyle = "american",
    settlement: Settlement = "physical",
    currency: str = "USD",
) -> OptionContract:
    """The contract a canonical id names. Raises ``ValueError`` on anything
    that is not one."""
    parts = contract_id.split(":")
    if len(parts) not in (4, 5):
        raise ValueError(f"not an option contract id: {contract_id!r}")
    underlying, expiry_text, right_code, strike_text = parts[:4]
    if right_code not in _RIGHT_CODE:
        raise ValueError(f"bad right {right_code!r} in {contract_id!r}")
    try:
        expiry = date.fromisoformat(expiry_text)
        strike = float(strike_text)
        multiplier = float(parts[4]) if len(parts) == 5 else STANDARD_MULTIPLIER
    except ValueError as exc:
        raise ValueError(f"not an option contract id: {contract_id!r}") from exc
    return OptionContract(
        underlying=underlying,
        expiry=expiry,
        strike=strike,
        right=_RIGHT_CODE[right_code],
        multiplier=multiplier,
        style=style,
        settlement=settlement,
        currency=currency,
    )


def is_option_id(ticker: str) -> bool:
    """True when ``ticker`` is a canonical option contract id."""
    try:
        parse_contract_id(ticker)
    except ValueError:
        return False
    return True


def occ_symbol(root: str, expiry: date, right: OptionRight, strike: float) -> str:
    """``AAPL  260116C00150000``: root padded to six characters, ``YYMMDD``,
    ``C`` or ``P``, and the strike in thousandths on eight digits."""
    if not root or len(root) > 6:
        raise ValueError(f"OCC root must be 1 to 6 characters, got {root!r}")
    if right not in _CODE_OF:
        raise ValueError(f"right must be call or put, got {right!r}")
    milli = round(strike * 1000)
    if milli <= 0 or milli >= 10**8:
        raise ValueError(f"strike out of OCC range: {strike}")
    return f"{root:<6}{expiry:%y%m%d}{_CODE_OF[right]}{milli:08d}"


def parse_occ_symbol(symbol: str) -> tuple[str, date, OptionRight, float]:
    """``(root, expiry, right, strike)`` of an OCC symbol. Accepts the padded
    21-character form and the compact form vendors often send
    (``AAPL260116C00150000``)."""
    text = symbol.strip()
    if len(text) < 16:
        raise ValueError(f"not an OCC symbol: {symbol!r}")
    tail = text[-15:]
    root = text[:-15].strip()
    date_text, right_code, strike_text = tail[:6], tail[6], tail[7:]
    if not root or len(root) > 6 or right_code not in _RIGHT_CODE or not strike_text.isdigit():
        raise ValueError(f"not an OCC symbol: {symbol!r}")
    try:
        expiry = datetime.strptime(date_text, "%y%m%d").date()
    except ValueError as exc:
        raise ValueError(f"not an OCC symbol: {symbol!r}") from exc
    return root, expiry, _RIGHT_CODE[right_code], int(strike_text) / 1000.0


def adjust_for_split(
    contract: OptionContract, quantity: float, ratio: float
) -> tuple[OptionContract, float]:
    """The contract and signed quantity after a split of ``ratio`` new
    shares per old share, following OCC practice:

    - a whole-number forward split (2:1, 4:1) multiplies the number of
      contracts by the ratio and divides the strike by it; the multiplier
      stays 100;
    - any other ratio (3:2, a reverse split) keeps the number of contracts,
      divides the strike by the ratio and multiplies the deliverable
      (``multiplier``) by it.

    Either way the deliverable (contracts x multiplier) grows with the
    shares by ``ratio`` and the total strike value (contracts x multiplier
    x strike) is unchanged."""
    if not (math.isfinite(ratio) and ratio > 0):
        raise ValueError(f"split ratio must be positive, got {ratio}")
    if ratio == 1.0:
        return contract, quantity
    whole = ratio >= 1.0 and float(ratio).is_integer()
    strike = round(contract.strike / ratio, 6)
    if whole:
        return replace(contract, strike=strike), quantity * ratio
    return replace(contract, strike=strike, multiplier=round(contract.multiplier * ratio, 6)), (
        quantity
    )
