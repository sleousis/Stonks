"""Protective puts bought only when the trend turns down (roadmap 17.5).

Holds round lots of each underlying and buys one put per 100 shares at
about ``delta`` with about ``dte`` days while the close is below its
``trend_days`` moving average. The puts are sold when the trend turns up
again or with ``roll_dte`` days left (then bought again if still needed).
"""

from __future__ import annotations

from stonks.core.params import ParameterSpec, ParamSpace
from stonks.options.strategies._common import (
    buy_shares,
    days_left,
    option_groups,
    sma,
)
from stonks.options.strategy import Decision, OptionDecisionContext, OptionIntent, OptionStrategy


class ProtectivePut(OptionStrategy):
    id = "protective_put"
    hypothesis = (
        "Puts are expensive on average, so owning them all the time bleeds "
        "the variance premium. Crashes cluster in downtrends (volatility "
        "clustering, Kindleberger), so buying puts only below the moving "
        "average pays for protection when it is most likely needed (P34). "
        "We give up premium to put sellers in exchange for a capped "
        "drawdown. It fails in sudden crashes from an uptrend and in "
        "whipsaws around the average."
    )
    structures = ("protective_put",)

    @classmethod
    def parameter_spec(cls) -> ParamSpace:
        return [
            ParameterSpec("delta", "float", 0.30, (0.05, 0.6), description="put delta to buy"),
            ParameterSpec("dte", "int", 60, (14, 180), description="target days to expiry"),
            ParameterSpec("trend_days", "int", 100, (20, 250), description="moving average"),
            ParameterSpec("stock_allocation", "float", 0.9, (0.1, 1.0), tunable=False),
            ParameterSpec("roll_dte", "int", 14, (0, 60)),
        ]

    def decide(self, ctx: OptionDecisionContext) -> list[Decision]:
        p = self.params
        out: list[Decision] = []
        underlyings = list(ctx.history)
        for underlying in underlyings:
            shares = buy_shares(ctx, underlying, p["stock_allocation"] / len(underlyings), self.id)
            if shares is not None:
                out.append(shares)
                continue
            closes = ctx.closes(underlying)
            average = sma(closes, p["trend_days"])
            risk_off = average is not None and closes[-1] < average
            groups = option_groups(ctx, underlying, self.structures)
            for group in groups:
                left = days_left(ctx, group)
                if not risk_off or (left is not None and left <= p["roll_dte"]):
                    out.append(
                        OptionIntent(
                            underlying,
                            "close_group",
                            group_id=group.group_id,
                            reason="trend up" if not risk_off else "roll",
                        )
                    )
            if risk_off and not groups and ctx.shares(underlying) >= 100:
                out.append(
                    OptionIntent(
                        underlying, "protective_put", {"delta": p["delta"], "dte": p["dte"]}
                    )
                )
        return out
