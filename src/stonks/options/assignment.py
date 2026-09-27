"""Early assignment of short American options (roadmap 17.3).

An :class:`AssignmentModel` looks at one short option position on one day
and says whether it is assigned early, and whether to flag the risk. The
default, :class:`ExtrinsicAssignmentModel`, follows how option holders
actually behave:

- a short call in the money is assigned the day before an ex-dividend
  date when its extrinsic value (mark minus intrinsic) is below the
  dividend: the holder exercises to collect the dividend;
- a short put deep in the money is assigned when its extrinsic value is
  below ``put_extrinsic_threshold`` (the holder gains the interest on the
  strike by exercising early).

Each check also raises a risk flag earlier, when the extrinsic value is
within ``flag_multiple`` times the trigger, so a report shows the
positions that were at risk even when they were not assigned. European
options are never assigned early. ``assign=False`` keeps the flags and
never assigns.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date

from stonks.core.options import OptionContract


@dataclass(frozen=True)
class AssignmentCheck:
    assign: bool
    flag: bool
    reason: str = ""


NO_RISK = AssignmentCheck(assign=False, flag=False)


class AssignmentModel(ABC):
    @abstractmethod
    def check(
        self,
        contract: OptionContract,
        quantity: float,
        as_of: date,
        spot: float,
        mark: float | None,
        next_ex_dividend: tuple[date, float] | None,
    ) -> AssignmentCheck:
        """``quantity`` is signed contracts; only negative (short) matters.
        ``next_ex_dividend`` is the next ex-date after ``as_of`` and its
        amount per share, when one is known."""


class NeverAssign(AssignmentModel):
    def check(self, contract, quantity, as_of, spot, mark, next_ex_dividend) -> AssignmentCheck:
        return NO_RISK


@dataclass(frozen=True)
class ExtrinsicAssignmentModel(AssignmentModel):
    assign: bool = True
    put_extrinsic_threshold: float = 0.05
    flag_multiple: float = 2.0
    #: Calendar days before the ex-date that count as "the day before"
    #: (3 covers a Friday before a Monday ex-date).
    dividend_window_days: int = 3

    def check(
        self,
        contract: OptionContract,
        quantity: float,
        as_of: date,
        spot: float,
        mark: float | None,
        next_ex_dividend: tuple[date, float] | None,
    ) -> AssignmentCheck:
        if quantity >= 0 or contract.style != "american" or mark is None:
            return NO_RISK
        intrinsic = contract.intrinsic(spot)
        if intrinsic <= 0:
            return NO_RISK
        extrinsic = max(mark - intrinsic, 0.0)
        if contract.is_call:
            if next_ex_dividend is None:
                return NO_RISK
            ex_date, amount = next_ex_dividend
            days = (ex_date - as_of).days
            if ex_date > contract.expiry or days <= 0 or amount <= 0:
                return NO_RISK
            reason = f"extrinsic {extrinsic:.2f} vs dividend {amount:.2f} going ex {ex_date}"
            if days <= self.dividend_window_days and extrinsic < amount:
                return AssignmentCheck(assign=self.assign, flag=True, reason=reason)
            if extrinsic < amount * self.flag_multiple:
                return AssignmentCheck(assign=False, flag=True, reason=reason)
            return NO_RISK
        reason = f"deep in the money put, extrinsic {extrinsic:.2f}"
        if extrinsic < self.put_extrinsic_threshold:
            return AssignmentCheck(assign=self.assign, flag=True, reason=reason)
        if extrinsic < self.put_extrinsic_threshold * self.flag_multiple:
            return AssignmentCheck(assign=False, flag=True, reason=reason)
        return NO_RISK
