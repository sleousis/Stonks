"""Settings of the account rules engine (roadmap 19.7).

They live next to the risk rules (config imports them) and sit under ``[production.risk.rules.account_rules]`` (the
``account_rules`` risk rule), so portfolio overrides can only tighten them
(``production.rules.settings.MERGE_RULES``).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: Business days from trade to settlement per market (EODHD exchange code).
#: US securities settle T+1. EU and UK markets settle T+2 today and plan to
#: move to T+1 in October 2027, so the cycle is a setting per market.
DEFAULT_SETTLEMENT_DAYS: dict[str, int] = {
    "default": 2,
    "US": 1,
    "TO": 1,
    "LSE": 2,
    "XETRA": 2,
    "F": 2,
    "PA": 2,
    "AS": 2,
    "BR": 2,
    "MI": 2,
    "MC": 2,
    "SW": 2,
}


class AccountRulesSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    settlement_days: dict[str, int] = Field(default_factory=lambda: dict(DEFAULT_SETTLEMENT_DAYS))
    #: US pattern day trader rule (margin accounts under the threshold).
    #: FINRA has proposed replacing it, so the numbers are settings.
    pdt_equity_threshold: float = Field(default=25_000.0, ge=0.0)
    pdt_max_day_trades: int = Field(default=3, ge=0)
    pdt_window_days: int = Field(default=5, ge=1)
    #: US wash sale window after a loss sale, in calendar days.
    wash_sale_window_days: int = Field(default=30, ge=0)
    #: EU and UK: keep each net short below this fraction of issued shares
    #: (0.1% must be reported to the regulator).
    short_disclosure_threshold: float = Field(default=0.001, gt=0.0, lt=1.0)
    #: Margin accounts (roadmap 19.13). Off by default: the first live
    #: account is a cash account, long only. While off, a margin profile
    #: cannot be chosen and a margin book opens nothing new.
    margin_accounts: bool = False
    #: Share of the account's equity that the what-if margin (initial and
    #: maintenance, after the order) must leave unused.
    margin_buffer: float = Field(default=0.10, ge=0.0, lt=1.0)

    @field_validator("settlement_days")
    @classmethod
    def _cycles(cls, value: dict[str, int]) -> dict[str, int]:
        if any(days < 0 for days in value.values()):
            raise ValueError("settlement days must be 0 or more")
        merged = dict(DEFAULT_SETTLEMENT_DAYS)
        merged.update({k.upper() if k != "default" else k: v for k, v in value.items()})
        return merged

    @property
    def active(self) -> bool:
        return self.enabled

    def cycle(self, market: str) -> int:
        return self.settlement_days.get(market.upper(), self.settlement_days["default"])


def longer_cycles(a: dict[str, int], b: dict[str, int]) -> dict[str, int]:
    """Per market, the longer settlement cycle (the stricter one)."""
    return {k: max(a.get(k, 0), b.get(k, 0)) for k in {*a, *b}}
